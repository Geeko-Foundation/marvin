"""Browser push notifications via django-webpush.

Who is told about a state change lives in profile/services/recipients.py,
shared with email. Comments and internal notes are routed here.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse

from profile.groups import GROUP_FINANCE, GROUP_TSP
from profile.models import muted
from profile.services.recipients import state_change_recipients
from tsp.models import RequestState, TravelRequest

logger = logging.getLogger('marvin.webpush')


def absolute_url(path: str) -> str:
    """Build a full URL from a path using ``settings.WEBPUSH_APP_BASE_URL``."""
    base = getattr(settings, 'WEBPUSH_APP_BASE_URL', '').rstrip('/')
    if not path.startswith('/'):
        path = f'/{path}'
    return f'{base}{path}'


def send_push_to_user(
    user: User,
    *,
    head: str,
    body: str,
    url: str | None = None,
    ttl: int = 86400,
    kind: str | None = None,
) -> None:
    """
    Send one web push to ``user`` when notifications are enabled and subscribed.

    No-op for inactive users, disabled preferences, or missing subscriptions.
    """
    if not user or not user.is_active or muted(user, kind):
        return
    # Subscribing a browser is how someone turns alerts on.
    if not user.webpush_info.exists():
        return

    from webpush import send_user_notification

    payload = {'head': head, 'body': body}
    if url:
        payload['url'] = url
    try:
        send_user_notification(user=user, payload=payload, ttl=ttl)
    except Exception:
        logger.exception('Push notification failed for user %s', user.pk)


def send_push_to_group(
    group_name: str,
    *,
    head: str,
    body: str,
    url: str | None = None,
    exclude_user: User | None = None,
    ttl: int = 86400,
    kind: str | None = None,
) -> None:
    """Send the same push payload to every member of a Django auth group."""
    from django.contrib.auth.models import Group

    try:
        group = Group.objects.get(name=group_name)
    except Group.DoesNotExist:
        return

    users = group.user_set.select_related('profile')
    if exclude_user is not None:
        users = users.exclude(pk=exclude_user.pk)

    for user in users:
        send_push_to_user(user, head=head, body=body, url=url, ttl=ttl, kind=kind)


def _request_label(travel_request: TravelRequest) -> str:
    """Short human-readable label for push notification body text."""
    return f'{travel_request.reference} · {travel_request.event.name}'


def _request_url(travel_request: TravelRequest) -> str:
    """Absolute URL to the travel request detail page for click-through."""
    path = reverse('request_detail', kwargs={'pk': travel_request.pk})
    return absolute_url(path)


# Title and body for each state. The recipients come from state_change_recipients.
PUSH_TEXT = {
    RequestState.SUBMITTED: ('New travel request', '{label} was submitted for review.'),
    RequestState.APPROVED: ('Request approved', '{label} was approved by the committee.'),
    RequestState.REJECTED: ('Request rejected', '{label} was rejected.{detail}'),
    RequestState.ACCEPTED: ('Request accepted', '{label} was accepted by the requester.'),
    RequestState.SUBMITTED_FOR_PAYMENT: ('Ready for payment', '{label} was submitted for reimbursement.'),
    RequestState.PAID: ('Payment processed', 'Reimbursement for {label} has been processed.'),
}


def notify_request_state_change(
    travel_request: TravelRequest,
    from_state: str | None,
    to_state: str,
    actor: User,
    notes: str = '',
) -> None:
    """Push to whoever needs to act next, the same people the email goes to."""
    recipients = state_change_recipients(travel_request, from_state, to_state, actor)
    if not recipients:
        return
    label = _request_label(travel_request)
    if to_state == RequestState.CANCELLED:
        by = 'requester' if getattr(actor, 'pk', None) == travel_request.user_id else 'committee'
        head, body = 'Request cancelled', f'{label} was cancelled by the {by}.'
    else:
        head, body = PUSH_TEXT[to_state]
        body = body.format(label=label, detail=f' Notes: {notes}' if notes else '')
    url = _request_url(travel_request)
    for user in recipients:
        send_push_to_user(user, kind=to_state, head=head, body=body, url=url)


def notify_request_comment(travel_request: TravelRequest, actor: User, internal: bool = False) -> None:
    """Notify the other party when a comment is added to a travel request."""
    label = _request_label(travel_request)
    url = _request_url(travel_request)

    if internal:
        # Tell everyone who can read internal notes: the committee, finance and
        # the event's organisers. Never the requester, even one on the committee.
        from django.contrib.auth.models import User as UserModel

        readers = UserModel.objects.filter(groups__name__in=[GROUP_TSP, GROUP_FINANCE])
        organisers = [u.pk for u in travel_request.event.organizers.all()
                      if travel_request.event.is_organised_by(u)]
        readers = (readers | UserModel.objects.filter(pk__in=organisers)).exclude(
            pk__in=[actor.pk, travel_request.user_id],
        ).distinct()
        for user in readers:
            send_push_to_user(
                user,
                kind='internal_note',
                head='New internal note',
                body=f'{actor.get_full_name() or actor.username} added an internal note on {label}.',
                url=url,
            )
        return

    if actor.pk == travel_request.user_id:
        send_push_to_group(
            GROUP_TSP,
            kind='comment', head='New comment',
            body=f'{actor.get_full_name() or actor.username} commented on {label}.',
            url=url,
            exclude_user=actor,
        )
    else:
        send_push_to_user(
            travel_request.user,
            kind='comment', head='New comment',
            body=f'{actor.get_full_name() or actor.username} commented on {label}.',
            url=url,
        )
