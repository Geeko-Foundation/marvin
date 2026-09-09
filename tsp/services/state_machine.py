from django.utils import timezone

from tsp.models import RequestState, StateHistory, TravelRequest


TERMINAL_STATES = {
    RequestState.REJECTED,
    RequestState.PAID,
    RequestState.CANCELLED,
}

TRANSITIONS = {
    RequestState.DRAFT: {RequestState.SUBMITTED, RequestState.CANCELLED},
    RequestState.SUBMITTED: {RequestState.APPROVED, RequestState.REJECTED, RequestState.CANCELLED},
    RequestState.APPROVED: {RequestState.ACCEPTED, RequestState.CANCELLED},
    RequestState.ACCEPTED: {RequestState.SUBMITTED_FOR_PAYMENT},
    RequestState.SUBMITTED_FOR_PAYMENT: {RequestState.PAID},
}


class StateTransitionError(Exception):
    pass


def can_transition(from_state, to_state):
    if from_state in TERMINAL_STATES:
        return False
    return to_state in TRANSITIONS.get(from_state, set())


def transition_request(travel_request, to_state, user, notes=''):
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

    StateHistory.objects.create(
        request=travel_request,
        from_state=from_state,
        to_state=to_state,
        user=user,
        notes=notes or None,
    )

    return travel_request


def create_request_with_history(travel_request, user):
    StateHistory.objects.create(
        request=travel_request,
        from_state=None,
        to_state=RequestState.DRAFT,
        user=user,
        notes='Request created',
    )
