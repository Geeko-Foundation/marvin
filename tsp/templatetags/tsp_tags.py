"""Template filters for TSP UI styling."""

import re

from django import template
from django.urls import reverse
from django.utils.dateformat import format as format_date
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

# The audit log names a request as "request 12", payments as "wise-api TSP12".
_REQUEST_MENTION = re.compile(r'\brequest (\d+)\b|\bTSP(\d+)\b')


@register.filter
def link_requests(text):
    """Turn each request mentioned in ``text`` into a TSP link to its page."""
    def link(match):
        pk = match.group(1) or match.group(2)
        return f'<a href="{reverse("request_detail", args=[pk])}" class="link link-primary">TSP{pk}</a>'
    return mark_safe(_REQUEST_MENTION.sub(link, escape(text or '')))

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


@register.filter
def state_label(state):
    """The readable name of a state, for values stored without their choices."""
    from tsp.models import RequestState

    try:
        return RequestState(state).label
    except ValueError:
        return str(state or '').replace('_', ' ').capitalize()


@register.filter
def date_range(start, end):
    """
    A span of dates as people write it: 1 to 3 May 2026.

    Repeats only the parts that change, so a range within one month names the
    month once, and a single day prints as one date.
    """
    if not start:
        return ''
    if not end or end == start:
        return format_date(start, 'j M Y')
    if (start.year, start.month) == (end.year, end.month):
        return f"{format_date(start, 'j')} to {format_date(end, 'j M Y')}"
    if start.year == end.year:
        return f"{format_date(start, 'j M')} to {format_date(end, 'j M Y')}"
    return f"{format_date(start, 'j M Y')} to {format_date(end, 'j M Y')}"


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
