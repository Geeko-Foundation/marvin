import base64

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Q

from django.shortcuts import redirect, render
from django.urls import reverse
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
from profile.models import NOTIFICATION_KINDS
from profile.two_factor import has_authenticator, two_factor_required
from marvin.mail import send_email
from tsp.accounts import read_email_change_link, send_email_change_link
from tsp.forms import PasswordResetRequestForm
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
            ask_to_confirm_email(request, form)
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
        'notification_kinds': [
            (kind, label, kind not in profile.muted_notifications)
            for kind, label, who in NOTIFICATION_KINDS if who & _roles(request.user, profile)
        ],
    })


def _roles(user, profile):
    """Which notification audiences ``user`` belongs to."""
    from tsp.models import Event
    roles = {name for name, on in (
        ('requester', profile.is_requester), ('tsp', profile.is_tsp), ('finance', profile.is_finance),
    ) if on}
    if Event.organised_by(user).exists():
        roles.add('organiser')
    return roles


@login_required
@require_POST
def notification_settings(request):
    """Save which kinds of alert and email the signed-in user wants."""
    profile = get_profile(request.user)
    wanted = set(request.POST.getlist('notify'))
    shown = {kind for kind, _, who in NOTIFICATION_KINDS if who & _roles(request.user, profile)}
    # Only the kinds on the form change; one hidden from this person keeps its setting.
    profile.muted_notifications = [
        kind for kind, _, _ in NOTIFICATION_KINDS
        if (kind in shown and kind not in wanted) or (kind not in shown and kind in profile.muted_notifications)
    ]
    profile.save(update_fields=['muted_notifications'])
    messages.success(request, 'Notification settings saved.')
    return redirect(reverse('profile') + '#notifications')


def ask_to_confirm_email(request, form):
    """Send the confirmation link when the form asked for a new email address."""
    if not form.new_email:
        return
    if send_email_change_link(request, request.user, form.new_email):
        messages.info(request, f'We sent a link to {form.new_email}. Your email changes once you open it.')
    else:
        messages.error(request, f'We could not send a link to {form.new_email}. Your email is unchanged; please try again later.')


def email_change_confirm(request, token):
    """Move an account to the email address the link was sent to."""
    link = read_email_change_link(token)
    user = link and User.objects.filter(pk=link[0], email=link[1]).first()
    taken = user and User.objects.filter(
        Q(email__iexact=link[2]) | Q(username__iexact=link[2])).exclude(pk=user.pk).exists()
    if not user or taken:
        messages.error(request, 'That link has expired or was already used. Change your email again to get a new one.')
        return redirect('profile')
    # Accounts that sign in with their email keep doing so with the new one.
    username_changed = user.username.lower() == link[1].lower()
    if username_changed:
        user.username = link[2].lower()
    user.email = link[2]
    user.save(update_fields=['email', 'username'])
    # The old address hears about it, so the owner learns if it was not them.
    send_email(link[1], f'Your {settings.BRAND_APP_NAME} email address was changed', 'email/email_changed_notice.txt', {
        'user': user, 'new_email': link[2], 'username_changed': username_changed,
    })
    audit('account.email_changed', user=user, target=user.username, detail=f'{link[1]} to {link[2]}')
    messages.success(request, f'Your email is now {user.email}.')
    return redirect('profile')


@login_required
@require_POST
def password_change_email(request):
    """
    Email a link for choosing a new password, the same way a reset works.

    Changing it only through the inbox means someone at an unlocked screen
    cannot lock the owner out of their own account.
    """
    form = PasswordResetRequestForm({'email': request.user.email})
    if form.is_valid():
        form.save(
            request=request,
            use_https=request.is_secure(),
            subject_template_name='email/password_change_subject.txt',
            email_template_name='email/password_change.txt',
        )
    if form.sent:
        messages.success(request, f'We have emailed {request.user.email} a link to choose a new password.')
    else:
        messages.error(request, 'We could not send the email just now. Please try again in a few minutes.')
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
        'config_url': device.config_url,
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
    messages.warning(request, 'Two-step sign-in is off. Turn it back on from this page to protect your account.')
    return redirect('profile')
