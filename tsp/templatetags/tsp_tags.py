"""Template filters for TSP UI styling."""

from django import template

register = template.Library()

BADGE_MAP = {
    'draft': 'badge-ghost',
    'submitted': 'badge-info',
    'approved': 'badge-warning',
    'rejected': 'badge-error',
    'accepted': 'badge-primary',
    'submitted_for_payment': 'badge-secondary',
    'paid': 'badge-success',
    'cancelled': 'badge-neutral',
}


@register.filter
def state_badge(state):
    """Return a DaisyUI badge CSS class for a travel request ``state`` value."""
    return BADGE_MAP.get(state, 'badge-ghost')


@register.simple_tag(takes_context=True)
def page_url(context, param, number):
    """Current query string with the page number swapped, so filters survive paging."""
    query = context['request'].GET.copy()
    query[param] = number
    return '?' + query.urlencode()


@register.simple_tag
def vapid_public_key():
    """The key browsers subscribe with, for the alerts button."""
    from django.conf import settings

    return settings.WEBPUSH_SETTINGS['VAPID_PUBLIC_KEY']
