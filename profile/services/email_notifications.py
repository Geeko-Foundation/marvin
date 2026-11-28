"""Email notifications for travel request activity.

These mirror the browser push notifications. Push only reaches someone who
granted permission and still has that browser subscribed, so email is what
most people will actually receive.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse

from marvin.mail import absolute_url, send_email
from profile.models import muted
from profile.services.recipients import state_change_recipients
from tsp.models import RequestState, TravelRequest


def request_url(travel_request: TravelRequest) -> str:
    return absolute_url(reverse('request_detail', kwargs={'pk': travel_request.pk}))


def send_to(recipients, *, subject: str, headline: str, travel_request, notes: str = '', kind: str | None = None) -> None:
    """One message per recipient, so nobody sees anyone else's address. Never raises."""
    for user in recipients:
        if not user or not user.is_active or not user.email or muted(user, kind):
            continue
        send_email(user.email, subject, 'email/request_state_change.txt', {
            'recipient': user,
            'headline': headline,
            'travel_request': travel_request,
            'notes': notes,
            'url': request_url(travel_request),
        })


# Subject and headline for each state. The recipients come from state_change_recipients.
EMAIL_TEXT = {
    RequestState.SUBMITTED: (
        'submitted for review', 'A travel request has been submitted and is waiting for the committee to review it.'),
    RequestState.APPROVED: (
        'approved', 'Your travel request has been approved. Please open it and accept the support to confirm you will attend.'),
    RequestState.REJECTED: ('not approved', 'Your travel request has not been approved.'),
    RequestState.ACCEPTED: (
        'accepted', 'The requester has accepted the approved support and will upload their receipts and documents after the trip.'),
    RequestState.SUBMITTED_FOR_PAYMENT: (
        'ready for payment', 'This request has its receipts and documents and is ready to be paid.'),
    RequestState.PAID: (
        'paid', 'Your reimbursement has been paid. It can take a few working days to reach your account.'),
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
        headline = ('The requester has cancelled this travel request.' if by_requester
                    else 'Your travel request has been cancelled by the committee.')
    else:
        subject, headline = EMAIL_TEXT[to_state]
    # Only the requester's own outcomes carry the reviewer's notes.
    keep_notes = to_state in (RequestState.APPROVED, RequestState.REJECTED, RequestState.CANCELLED)
    send_to(
        recipients,
        kind=to_state, subject=f'Travel request {travel_request.reference} for {travel_request.event.name} {subject}',
        headline=headline, travel_request=travel_request, notes=notes if keep_notes else '',
    )
