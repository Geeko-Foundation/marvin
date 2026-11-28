"""Redirect requestors through onboarding until profile and Wise setup are complete."""

from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from profile.decorators import get_profile
from profile.services.wise_verification import verification_state

ONBOARDING_URL_NAMES = {
    'signup',
    'signup_account',
    'signup_activate',
    'signup_resend',
    'signup_profile',
    'signup_banking',
    'signup_banking_options',
    'signup_banking_progress',
    'signup_banking_step',
    'signup_skip_banking',
    'signup_complete',
    'onboarding_personal',
    'onboarding_wise',
    'check_wise_account',
    'login',
    'logout',
    # Two-step sign-in comes first: its middleware sends people here, so
    # sending them back to onboarding would bounce between the two forever.
    'two_factor_setup',
    'two_factor_verify',
    'two_factor_backup_codes',
}


def _is_exempt(request) -> bool:
    """
    Return True when onboarding enforcement should not run for this request.

    Exempt paths include admin, static, auth, signup wizard, payments, and
    unauthenticated requests.
    """
    if request.path_info.startswith('/admin/'):
        return True

    if not request.user.is_authenticated:
        return True

    try:
        match = resolve(request.path_info)
    except Resolver404:
        return False

    if match.url_name == 'signup':
        return True

    if match.app_name == 'payments':
        return True

    if match.url_name in ONBOARDING_URL_NAMES:
        return True

    if request.path_info.startswith('/static/') or request.path_info.startswith('/uploads/'):
        return True

    return False


class RequesterOnboardingMiddleware:
    """Redirect incomplete requestors to their current onboarding step."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        """Enforce onboarding before passing the request to downstream views."""
        if not _is_exempt(request):
            profile = get_profile(request.user)
            # Committee and finance users often hold Requester too. Sending them
            # through the payout wizard would block their review work, so the
            # requirement is enforced when they create or submit a request.
            if profile.is_requester and not profile.is_committee:
                step = profile.onboarding_step()
                if step:
                    try:
                        current = resolve(request.path_info).url_name
                    except Resolver404:
                        current = None
                    if current != step:
                        return redirect(step)

        return self.get_response(request)


class StaleBankDetailsMiddleware:
    """Drop bank details left in the session by a verification nobody finished."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        """Checked on every request, so they go even if the banking pages are never opened again."""
        verification_state(request.session)
        return self.get_response(request)
