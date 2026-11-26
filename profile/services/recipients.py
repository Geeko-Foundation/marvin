"""Who is told when a travel request changes state.

Push and email both read this list, so the two channels cannot drift apart.
"""

from __future__ import annotations

from django.contrib.auth.models import User

from profile.groups import GROUP_FINANCE, GROUP_TSP
from tsp.models import RequestState, TravelRequest


def _group(name: str, actor_pk) -> list[User]:
    return list(User.objects.filter(groups__name=name).exclude(pk=actor_pk).select_related('profile'))


def state_change_recipients(travel_request: TravelRequest, from_state: str | None, to_state: str, actor) -> list[User]:
    """Everyone who should hear about this transition, without the actor and without repeats."""
    requester = travel_request.user
    actor_pk = getattr(actor, 'pk', None)

    if from_state == RequestState.SUBMITTED_FOR_PAYMENT and to_state == RequestState.ACCEPTED:
        # The requester took it back to edit; "accepted" would mislead them.
        return []
    if to_state == RequestState.SUBMITTED:
        event = travel_request.event
        users = _group(GROUP_TSP, actor_pk) + [
            user for user in event.organizers.exclude(pk=actor_pk) if event.is_organised_by(user)
        ]
    elif to_state in (RequestState.APPROVED, RequestState.REJECTED, RequestState.PAID):
        users = [requester]
    elif to_state == RequestState.ACCEPTED:
        users = _group(GROUP_TSP, actor_pk)
    elif to_state == RequestState.SUBMITTED_FOR_PAYMENT:
        users = _group(GROUP_FINANCE, actor_pk)
    elif to_state == RequestState.CANCELLED:
        users = _group(GROUP_TSP, actor_pk) if actor_pk == requester.pk else [requester]
    else:
        users = []
    return list({user.pk: user for user in users}.values())
