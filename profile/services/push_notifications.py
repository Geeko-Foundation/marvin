"""Browser push notifications via django-webpush.

Suggested events (implemented below):
- Request submitted          → TSP Committee
- Request approved / rejected → Requester
- Request accepted           → Requester (upload documents reminder)
- Submitted for payment      → Finance
- Request paid               → Requester
- Request cancelled            → other party (requester or committee)
- New comment on request     → other party
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse

from profile.groups import GROUP_FINANCE, GROUP_TSP
from profile.models import muted
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


def notify_request_state_change(
    travel_request: TravelRequest,
    from_state: str | None,
    to_state: str,
    actor: User,
    notes: str = '',
) -> None:
    """
    Notify the appropriate party when a travel request changes state.

    Called from the TSP state machine; routes to requester or committee/finance
    groups depending on ``to_state``.
    """
    label = _request_label(travel_request)
    url = _request_url(travel_request)
    requester = travel_request.user

    if from_state == RequestState.SUBMITTED_FOR_PAYMENT and to_state == RequestState.ACCEPTED:
        # The requester took it back to edit; "accepted" would mislead them.
        return
    if to_state == RequestState.SUBMITTED:
        send_push_to_group(
            GROUP_TSP,
            kind=to_state, head='New travel request',
            body=f'{label} was submitted for review.',
            url=url,
            exclude_user=actor,
        )
        for organiser in travel_request.event.organizers.exclude(pk=getattr(actor, 'pk', None)):
            if travel_request.event.is_organised_by(organiser):
                send_push_to_user(organiser, kind=to_state, head='New travel request',
                                  body=f'{label} was submitted for review.', url=url)
    elif to_state == RequestState.APPROVED:
        send_push_to_user(
            requester,
            kind=to_state, head='Request approved',
            body=f'{label} was approved by the committee.',
            url=url,
        )
    elif to_state == RequestState.REJECTED:
        detail = f' Notes: {notes}' if notes else ''
        send_push_to_user(
            requester,
            kind=to_state, head='Request rejected',
            body=f'{label} was rejected.{detail}',
            url=url,
        )
    elif to_state == RequestState.ACCEPTED:
        # The requester accepts, so telling them is telling them what they
        # just did. The committee is who needs to know the trip is confirmed.
        send_push_to_group(
            GROUP_TSP,
            kind=to_state, head='Request accepted',
            body=f'{label} was accepted by the requester.',
            url=url,
            exclude_user=actor,
        )
    elif to_state == RequestState.SUBMITTED_FOR_PAYMENT:
        send_push_to_group(
            GROUP_FINANCE,
            kind=to_state, head='Ready for payment',
            body=f'{label} was submitted for reimbursement.',
            url=url,
            exclude_user=actor,
        )
    elif to_state == RequestState.PAID:
        send_push_to_user(
            requester,
            kind=to_state, head='Payment processed',
            body=f'Reimbursement for {label} has been processed.',
            url=url,
        )
    elif to_state == RequestState.CANCELLED:
        if actor.pk == requester.pk:
            send_push_to_group(
                GROUP_TSP,
                kind=to_state, head='Request cancelled',
                body=f'{label} was cancelled by the requester.',
                url=url,
                exclude_user=actor,
            )
        else:
            send_push_to_user(
                requester,
                kind=to_state, head='Request cancelled',
                body=f'{label} was cancelled by the committee.',
                url=url,
            )


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
