"""Email notifications for travel request activity.

These mirror the browser push notifications. Push only reaches someone who
granted permission and still has that browser subscribed, so email is what
most people will actually receive.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.urls import reverse

from profile.groups import GROUP_FINANCE, GROUP_TSP
from tsp.models import RequestState, TravelRequest

logger = logging.getLogger('marvin.email')


def request_url(travel_request: TravelRequest) -> str:
    base = getattr(settings, 'SITE_BASE_URL', '').rstrip('/')
    return f"{base}{reverse('request_detail', kwargs={'pk': travel_request.pk})}"


def group_members(group_name: str, exclude: list[User] | None = None):
    try:
        members = Group.objects.get(name=group_name).user_set.all()
    except Group.DoesNotExist:
        return []
    excluded = {user.pk for user in (exclude or []) if user is not None}
    return [user for user in members if user.pk not in excluded]


def send_to(recipients, *, subject: str, headline: str, travel_request, notes: str = '') -> None:
    """One message per recipient, so nobody sees anyone else's address."""
    try:
        body = render_to_string('email/request_state_change.txt', {
            'headline': headline,
            'travel_request': travel_request,
            'notes': notes,
            'url': request_url(travel_request),
        })
    except Exception:
        # Same reasoning as a send failure below: never break a transition.
        logger.exception('Could not render the notification email for request %s', travel_request.pk)
        return

    for user in recipients:
        if not user or not user.is_active or not user.email:
            continue
        try:
            EmailMessage(subject=subject, body=body, to=[user.email]).send()
        except Exception:
            # A mail server being down must not roll back a state transition.
            logger.exception('Notification email to user %s failed', user.pk)


def notify_request_state_change_email(
    travel_request: TravelRequest,
    from_state: str | None,
    to_state: str,
    actor: User,
    notes: str = '',
) -> None:
    """Email whoever needs to act next, matching the push notification rules."""
    requester = travel_request.user
    label = f'#{travel_request.pk} {travel_request.event.name}'

    if from_state == RequestState.SUBMITTED_FOR_PAYMENT and to_state == RequestState.ACCEPTED:
        # The requester took it back to edit; "accepted" would mislead them.
        return
    if to_state == RequestState.SUBMITTED:
        send_to(
            group_members(GROUP_TSP, exclude=[actor]),
            subject=f'Travel request {label} submitted for review',
            headline='A travel request is waiting for the committee to review it.',
            travel_request=travel_request,
        )
    elif to_state == RequestState.APPROVED:
        send_to(
            [requester],
            subject=f'Travel request {label} approved',
            headline='Your travel request was approved. Accept it to confirm you are attending.',
            travel_request=travel_request, notes=notes,
        )
    elif to_state == RequestState.REJECTED:
        send_to(
            [requester],
            subject=f'Travel request {label} rejected',
            headline='Your travel request was rejected.',
            travel_request=travel_request, notes=notes,
        )
    elif to_state == RequestState.ACCEPTED:
        send_to(
            [requester],
            subject=f'Travel request {label} accepted',
            headline='Thanks for accepting. Upload your receipts and signed form when you have them.',
            travel_request=travel_request,
        )
    elif to_state == RequestState.SUBMITTED_FOR_PAYMENT:
        send_to(
            group_members(GROUP_FINANCE, exclude=[actor]),
            subject=f'Travel request {label} ready for payment',
            headline='A travel request has its documents in and is ready to be paid.',
            travel_request=travel_request,
        )
    elif to_state == RequestState.PAID:
        send_to(
            [requester],
            subject=f'Travel request {label} paid',
            headline='Your reimbursement has been processed.',
            travel_request=travel_request,
        )
    elif to_state == RequestState.CANCELLED:
        if actor.pk == requester.pk:
            send_to(
                group_members(GROUP_TSP, exclude=[actor]),
                subject=f'Travel request {label} cancelled',
                headline='The requester cancelled their travel request.',
                travel_request=travel_request, notes=notes,
            )
        else:
            send_to(
                [requester],
                subject=f'Travel request {label} cancelled',
                headline='Your travel request was cancelled by the committee.',
                travel_request=travel_request, notes=notes,
            )
