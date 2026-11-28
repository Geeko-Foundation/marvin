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


# The road a request travels, in order, with what the page calls each stage.
PROGRESS_STAGES = [
    ('submitted', 'Submitted'),
    ('approved', 'Approved'),
    ('accepted', 'Accepted'),
    ('submitted_for_payment', 'Receipts in'),
    ('paid', 'Paid'),
]

# Who the request is waiting on, said plainly, for each state.
WAITING_ON = {
    'draft': 'Not sent yet. Submit it when it is ready, and the committee will review it.',
    'submitted': 'Waiting for the committee to review it.',
    'approved': 'Waiting for the requester to accept the approved support.',
    'accepted': 'Waiting for the requester to upload receipts and documents after the trip.',
    'submitted_for_payment': 'Waiting for finance to pay it.',
    'paid': 'Done. The reimbursement has been paid.',
    'rejected': 'Not approved. Nothing more is needed.',
    'cancelled': 'Cancelled. Nothing more is needed.',
}


@register.inclusion_tag('tsp/requests/_progress.html')
def request_progress(travel_request, history=()):
    """
    Where a request is on its way to being paid, what it is waiting on, and
    its history: when each stage was reached and by whom, then every change.
    ``history`` is the page's list, already trimmed for what this viewer may read.
    """
    state = travel_request.state
    reached_at = {entry.to_state: entry for entry in history}
    order = [key for key, _ in PROGRESS_STAGES]
    if state in order:
        reached = order.index(state)
    else:
        # Rejected, cancelled or a draft: show how far it got before stopping.
        been = {h.to_state for h in travel_request.state_history.all()}
        reached = max((i for i, key in enumerate(order) if key in been), default=-1)
    stopped = state in ('rejected', 'cancelled')
    steps = [{'label': label, 'done': i <= reached, 'current': i == reached and not stopped,
              'entry': reached_at.get(key) if i <= reached else None}
             for i, (key, label) in enumerate(PROGRESS_STAGES)]
    if stopped:
        steps = steps[:reached + 1] + [{'label': 'Not approved' if state == 'rejected' else 'Cancelled',
                                        'done': True, 'current': True, 'stopped': True,
                                        'entry': reached_at.get(state)}]
    waiting = WAITING_ON.get(state, '')
    payment = travel_request.payment
    if state == 'submitted_for_payment' and payment and payment.status not in ('failed', 'cancelled', 'exported'):
        waiting = 'The transfer is on its way. It is marked paid once Wise confirms it.'
    if state == 'accepted':
        waiting += f' Claims close on {travel_request.event.reimbursement_deadline():%d %b %Y}.'.replace(' 0', ' ')
    return {'steps': steps, 'waiting': waiting, 'stopped': stopped, 'history': history,
            'done_count': sum(s['done'] and not s.get('stopped') for s in steps), 'total': len(PROGRESS_STAGES)}
