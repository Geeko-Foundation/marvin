"""Names and theme shown in the interface, so a fork can rebrand from config."""

from django.conf import settings


def branding(request):
    """Expose the branding settings to every template."""
    return {
        'app_name': settings.BRAND_APP_NAME,
        'org_name': settings.BRAND_ORG_NAME,
        'app_tagline': settings.BRAND_TAGLINE,
        'app_strapline': settings.BRAND_STRAPLINE,
        'theme_light': settings.BRAND_THEME_LIGHT,
        'theme_dark': settings.BRAND_THEME_DARK,
    }
