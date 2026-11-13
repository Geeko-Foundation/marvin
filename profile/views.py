import base64

import qrcode
import qrcode.image.svg
from django.contrib import messages
from django.contrib.auth.forms import PasswordResetForm
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_otp import devices_for_user, match_token
from django_otp import login as otp_login
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from profile.audit_log import record as audit
from profile.decorators import get_profile
from profile.forms import ProfileForm, StaffBankingForm
from profile.two_factor import has_authenticator, two_factor_required
from tsp.decorators import login_required


@login_required
def profile_view(request):
    """
    Display and update the signed-in user's profile.

    Requestors edit their personal details, and staff may also submit banking
    details through a separate form. Stamps ``profile_completed_at`` when a
    requestor first completes the required fields. Alerts are turned on and off
    by the browser button on the page, not through this form.
    """
    profile = get_profile(request.user)
    banking_form = None

    if request.method == 'POST' and not profile.is_requester and 'banking_submit' in request.POST:
        banking_form = StaffBankingForm(request.POST, instance=profile)
        form = ProfileForm(instance=profile, user=request.user)
        if banking_form.is_valid():
            banking_form.save()
            messages.success(request, 'Banking details updated.')
            return redirect('profile')
    elif request.method == 'POST':
        form = ProfileForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            form.save(request.user)
            profile.refresh_from_db()
            if profile.is_requester and profile.is_profile_complete() and not profile.profile_completed_at:
                profile.profile_completed_at = timezone.now()
                profile.save(update_fields=['profile_completed_at'])
            messages.success(request, 'Profile updated successfully.')
            return redirect('profile')
    else:
        form = ProfileForm(instance=profile, user=request.user)

    if not profile.is_requester and banking_form is None:
        banking_form = StaffBankingForm(instance=profile)

    return render(request, 'profile/profile.html', {
        'form': form,
        'banking_form': banking_form,
        'profile': profile,
        'two_factor_on': has_authenticator(request.user),
        'two_factor_required': two_factor_required(request.user),
    })


@login_required
@require_POST
def password_change_email(request):
    """
    Email a link for choosing a new password, the same way a reset works.

    Changing it only through the inbox means someone at an unlocked screen
    cannot lock the owner out of their own account.
    """
    form = PasswordResetForm({'email': request.user.email})
    if form.is_valid():
        form.save(
            request=request,
            use_https=request.is_secure(),
            subject_template_name='email/password_change_subject.txt',
            email_template_name='email/password_change.txt',
        )
    messages.success(request, f'We have emailed {request.user.email} a link to choose a new password.')
    return redirect('profile')


BACKUP_CODE_COUNT = 10


def _safe_next(request):
    target = request.POST.get('next') or request.GET.get('next')
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
        return target
    return 'dashboard'


def _new_backup_codes(user):
    """Replace the user's backup codes with fresh ones, and return them to show once."""
    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name='Backup codes')
    codes = [StaticToken.random_token() for _ in range(BACKUP_CODE_COUNT)]
    for code in codes:
        device.token_set.create(token=code)
    return codes


@login_required
def two_factor_setup(request):
    """Link an authenticator app: show its QR code, then confirm with one code from it."""
    # Only someone who has passed the existing app's code may link another,
    # or a stolen password alone would be enough to take over the account.
    if has_authenticator(request.user):
        return redirect('profile' if request.user.is_verified() else 'two_factor_verify')
    device = TOTPDevice.objects.filter(user=request.user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=request.user, name='Authenticator app', confirmed=False)
    error = None
    if request.method == 'POST':
        if device.verify_token(request.POST.get('token', '').replace(' ', '')):
            device.confirmed = True
            device.save(update_fields=['confirmed'])
            otp_login(request, device)
            audit('two_factor.enabled', user=request.user, target=request.user.username)
            return render(request, 'profile/two_factor_backup_codes.html', {
                'codes': _new_backup_codes(request.user), 'next': _safe_next(request),
            })
        error = 'That code did not match. Check the time on your phone and try the newest code.'
    qr = qrcode.make(device.config_url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return render(request, 'profile/two_factor_setup.html', {
        'qr_svg': qr.to_string(encoding='unicode'),
        'secret': base64.b32encode(device.bin_key).decode(),
        'required': two_factor_required(request.user),
        'next': request.GET.get('next', ''),
        'error': error,
    })


@login_required
def two_factor_verify(request):
    """The second step of signing in: a code from the app, or a backup code."""
    if request.user.is_verified():
        return redirect(_safe_next(request))
    error = None
    if request.method == 'POST':
        device = match_token(request.user, request.POST.get('token', '').replace(' ', ''))
        if device:
            otp_login(request, device)
            return redirect(_safe_next(request))
        error = 'That code did not work. Codes change every 30 seconds, so try the newest one.'
    return render(request, 'profile/two_factor_verify.html', {
        'next': request.GET.get('next', ''), 'error': error,
    })


@login_required
@require_POST
def two_factor_backup_codes(request):
    """New backup codes, for when the old ones are used up or lost."""
    return render(request, 'profile/two_factor_backup_codes.html', {
        'codes': _new_backup_codes(request.user), 'next': 'profile',
    })


@login_required
@require_POST
def two_factor_disable(request):
    """Turn two-step sign-in off, for those who are not required to use it."""
    if two_factor_required(request.user):
        messages.error(request, 'Your role needs two-step sign-in, so it cannot be turned off.')
        return redirect('profile')
    for device in devices_for_user(request.user, confirmed=None):
        device.delete()
    audit('two_factor.disabled', user=request.user, target=request.user.username)
    messages.success(request, 'Two-step sign-in is off.')
    return redirect('profile')
