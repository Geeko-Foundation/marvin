"""Two-step sign-in with an authenticator app, through django-otp."""

from django.conf import settings
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils.http import urlencode
from django_otp import devices_for_user

from profile.decorators import get_profile

# Pages someone part way through signing in can still reach.
EXEMPT_URL_NAMES = {'two_factor_setup', 'two_factor_verify', 'logout'}


def two_factor_required(user):
    """Anyone who can approve requests or move money has to use it."""
    return user.is_superuser or user.is_staff or get_profile(user).is_committee


def has_authenticator(user):
    return any(devices_for_user(user, confirmed=True))


class TwoFactorMiddleware:
    """
    Hold a signed-in user at the code prompt until they pass it.

    Runs after django-otp's middleware, which tells it whether this session
    has already been verified with a code.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (settings.TWO_FACTOR_ENFORCED and user.is_authenticated
                and not user.is_verified() and not self._exempt(request)):
            target = 'two_factor_verify' if has_authenticator(user) else (
                'two_factor_setup' if two_factor_required(user) else None)
            if target:
                response = redirect(target)
                response['Location'] += '?' + urlencode({'next': request.get_full_path()})
                return response
        return self.get_response(request)

    @staticmethod
    def _exempt(request):
        if request.path_info.startswith(('/static/', '/admin/logout/')):
            return True
        try:
            return resolve(request.path_info).url_name in EXEMPT_URL_NAMES
        except Resolver404:
            return False
