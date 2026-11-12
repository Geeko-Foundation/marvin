"""Travel request lifecycle transitions and audit history."""

from django.utils import timezone

from profile.audit_log import record
from tsp.models import RequestState, StateHistory

TERMINAL_STATES = {
    RequestState.REJECTED,
    RequestState.PAID,
    RequestState.CANCELLED,
}

TRANSITIONS = {
    RequestState.DRAFT: {RequestState.SUBMITTED, RequestState.CANCELLED},
    RequestState.SUBMITTED: {RequestState.APPROVED, RequestState.REJECTED, RequestState.CANCELLED},
    # Back to submitted when the requester changes the amounts after approval.
    RequestState.APPROVED: {RequestState.ACCEPTED, RequestState.CANCELLED, RequestState.SUBMITTED},
    RequestState.ACCEPTED: {
        RequestState.SUBMITTED_FOR_PAYMENT, RequestState.SUBMITTED, RequestState.CANCELLED,
    },
    # Back to accepted when the requester takes it back to fix their costs.
    RequestState.SUBMITTED_FOR_PAYMENT: {RequestState.PAID, RequestState.ACCEPTED},
}


class StateTransitionError(Exception):
    """Raised when a requested state change is not allowed by ``TRANSITIONS``."""


def can_transition(from_state, to_state):
    """Return True when ``to_state`` is a valid next state from ``from_state``."""
    if from_state in TERMINAL_STATES:
        return False
    return to_state in TRANSITIONS.get(from_state, set())


def transition_request(travel_request, to_state, user, notes=''):
    """
    Move a travel request to ``to_state``, record history, and notify via push.

    Sets ``approved_at`` / ``accepted_at`` timestamps when entering approved or
    accepted states. Raises ``StateTransitionError`` on invalid transitions.
    """
    from_state = travel_request.state
    if not can_transition(from_state, to_state):
        raise StateTransitionError(
            f'Cannot transition from {from_state} to {to_state}'
        )

    travel_request.state = to_state
    now = timezone.now()

    if to_state == RequestState.APPROVED:
        travel_request.approved_at = now
    elif to_state == RequestState.ACCEPTED:
        travel_request.accepted_at = now
        travel_request.accepted_by = user

    travel_request.save()

    record(
        'request.state_change',
        user=user,
        target=f'request {travel_request.pk}',
        detail=f'{from_state} -> {to_state}' + (f': {notes}' if notes else ''),
    )

    StateHistory.objects.create(
        request=travel_request,
        from_state=from_state,
        to_state=to_state,
        user=user,
        notes=notes or None,
    )

    from profile.services.push_notifications import notify_request_state_change
    notify_request_state_change(travel_request, from_state, to_state, user, notes)

    from profile.services.email_notifications import notify_request_state_change_email
    notify_request_state_change_email(travel_request, from_state, to_state, user, notes)

    return travel_request


def create_request_with_history(travel_request, user):
    """Record the initial draft state when a new travel request is created."""
    StateHistory.objects.create(
        request=travel_request,
        from_state=None,
        to_state=RequestState.DRAFT,
        user=user,
        notes='Request created',
    )
