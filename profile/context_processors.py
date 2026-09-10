"""Template context helpers injected globally via ``settings.TEMPLATES``."""

from profile.decorators import get_profile


def user_profile(request):
    """Expose ``profile`` on every template for the signed-in user (or ``None``)."""
    if request.user.is_authenticated:
        return {'profile': get_profile(request.user)}
    return {'profile': None}
