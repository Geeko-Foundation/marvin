"""Redirect requestors through onboarding until profile and Wise setup are complete."""

from django.shortcuts import redirect
from django.urls import resolve

from profile.decorators import get_profile


ONBOARDING_URL_NAMES = {
    'signup',
    'signup_account',
    'signup_profile',
    'signup_banking',
    'signup_banking_progress',
    'signup_banking_step',
    'signup_skip_banking',
    'signup_complete',
    'onboarding_personal',
    'onboarding_wise',
    'check_wise_account',
    'login',
    'logout',
}


def _is_exempt(request) -> bool:
    if request.path_info.startswith('/admin/'):
        return True

    if not request.user.is_authenticated:
        return True

    try:
        match = resolve(request.path_info)
    except Exception:
        return request.path_info.startswith('/admin/')

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
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _is_exempt(request):
            profile = get_profile(request.user)
            if profile.is_requester:
                step = profile.onboarding_step()
                if step:
                    try:
                        current = resolve(request.path_info).url_name
                    except Exception:
                        current = None
                    if current != step:
                        return redirect(step)

        return self.get_response(request)
