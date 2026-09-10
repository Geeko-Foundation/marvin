from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from profile.decorators import get_profile
from profile.forms import ProfileForm, StaffBankingForm
from profile.services.push_notifications import clear_push_subscriptions
from tsp.decorators import login_required


@login_required
def profile_view(request):
    """
    Display and update the signed-in user's profile.

    Requestors edit personal details and push notification preferences. Staff
    members may additionally submit banking metadata via a separate form.
    Clears push subscriptions when notifications are disabled, and stamps
    ``profile_completed_at`` when a requestor first completes required fields.
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
        was_push_enabled = profile.push_notifications_enabled
        form = ProfileForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            form.save(request.user)
            profile.refresh_from_db()
            if was_push_enabled and not profile.push_notifications_enabled:
                clear_push_subscriptions(request.user)
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
    })
