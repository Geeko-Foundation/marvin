"""Email notifications for travel request activity.

These mirror the browser push notifications. Push only reaches someone who
granted permission and still has that browser subscribed, so email is what
most people will actually receive.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.urls import reverse

from profile.models import muted
from profile.services.recipients import state_change_recipients
from tsp.models import RequestState, TravelRequest

logger = logging.getLogger('marvin.email')


def request_url(travel_request: TravelRequest) -> str:
    base = getattr(settings, 'SITE_BASE_URL', '').rstrip('/')
    return f"{base}{reverse('request_detail', kwargs={'pk': travel_request.pk})}"


def send_to(recipients, *, subject: str, headline: str, travel_request, notes: str = '', kind: str | None = None) -> None:
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
        if not user or not user.is_active or not user.email or muted(user, kind):
            continue
        try:
            EmailMessage(subject=subject, body=body, to=[user.email]).send()
        except Exception:
            # A mail server being down must not roll back a state transition.
            logger.exception('Notification email to user %s failed', user.pk)


# Subject and headline for each state. The recipients come from state_change_recipients.
EMAIL_TEXT = {
    RequestState.SUBMITTED: ('submitted for review', 'A travel request is waiting for the committee to review it.'),
    RequestState.APPROVED: ('approved', 'Your travel request was approved. Accept it to confirm you are attending.'),
    RequestState.REJECTED: ('rejected', 'Your travel request was rejected.'),
    RequestState.ACCEPTED: ('accepted', 'The requester has accepted and will upload their documents.'),
    RequestState.SUBMITTED_FOR_PAYMENT: ('ready for payment', 'A travel request has its documents in and is ready to be paid.'),
    RequestState.PAID: ('paid', 'Your reimbursement has been processed.'),
}


def notify_request_state_change_email(
    travel_request: TravelRequest,
    from_state: str | None,
    to_state: str,
    actor: User,
    notes: str = '',
) -> None:
    """Email whoever needs to act next, the same people the push goes to."""
    recipients = state_change_recipients(travel_request, from_state, to_state, actor)
    if not recipients:
        return
    if to_state == RequestState.CANCELLED:
        by_requester = getattr(actor, 'pk', None) == travel_request.user_id
        subject = 'cancelled'
        headline = ('The requester cancelled their travel request.' if by_requester
                    else 'Your travel request was cancelled by the committee.')
    else:
        subject, headline = EMAIL_TEXT[to_state]
    # Only the requester's own outcomes carry the reviewer's notes.
    keep_notes = to_state in (RequestState.APPROVED, RequestState.REJECTED, RequestState.CANCELLED)
    send_to(
        recipients,
        kind=to_state, subject=f'Travel request #{travel_request.pk} {travel_request.event.name} {subject}',
        headline=headline, travel_request=travel_request, notes=notes if keep_notes else '',
    )
