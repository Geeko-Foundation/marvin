"""Template context helpers injected globally via ``settings.TEMPLATES``."""

from django.conf import settings

from profile.decorators import get_profile


def user_profile(request):
    """Expose ``profile`` for the signed-in user (or ``None``), and whether Wise is in use."""
    profile = get_profile(request.user) if request.user.is_authenticated else None
    return {'profile': profile, 'use_wise': settings.USE_WISE}
