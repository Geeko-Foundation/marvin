from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from profile.groups import GROUP_TSP, assign_user_to_group
from profile.models import UserProfile
from tsp.models import Event, Expense, RequestState, StateHistory, TravelRequest
from tsp.services.state_machine import (
    StateTransitionError,
    can_transition,
    transition_request,
)


def make_event():
    return Event.objects.create(
        name='openSUSE Conference',
        location='Nuremberg',
        start_date=date(2027, 6, 14),
        end_date=date(2027, 6, 16),
    )


def make_requester(username):
    """A requester who has finished onboarding, so the middleware lets them through."""
    user = User.objects.create_user(
        username, password='x', email=f'{username}@example.org', first_name='Test',
    )
    profile = UserProfile.objects.create(
        user=user, address_line='1 Main St', city='Pune', country='India',
        wise_account_id=1, wise_recipient_verified_at=timezone.now(),
    )
    profile.user.groups.clear()
    assign_user_to_group(user, 'Requester')
    return user


def make_committee(username):
    """Give committee users a profile up front: get_profile() files anyone
    without one under Requester, which would send them into onboarding."""
    user = User.objects.create_user(username, password='x')
    UserProfile.objects.create(user=user)
    assign_user_to_group(user, GROUP_TSP)
    return user


class StateMachineTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user('req', password='x')
        self.committee = User.objects.create_user('tspuser', password='x')
        self.request = TravelRequest.objects.create(
            user=self.requester, event=make_event(), description='Talk',
        )

    def test_happy_path_reaches_paid(self):
        for state in (
            RequestState.SUBMITTED,
            RequestState.APPROVED,
            RequestState.ACCEPTED,
            RequestState.SUBMITTED_FOR_PAYMENT,
            RequestState.PAID,
        ):
            transition_request(self.request, state, self.committee)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.PAID)
        self.assertEqual(StateHistory.objects.filter(request=self.request).count(), 5)

    def test_cannot_skip_review(self):
        with self.assertRaises(StateTransitionError):
            transition_request(self.request, RequestState.APPROVED, self.committee)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.DRAFT)
        self.assertFalse(StateHistory.objects.filter(request=self.request).exists())

    def test_terminal_states_are_final(self):
        for terminal in (RequestState.REJECTED, RequestState.PAID, RequestState.CANCELLED):
            for target in RequestState.values:
                self.assertFalse(can_transition(terminal, target), f'{terminal} -> {target}')

    def test_approve_and_accept_record_who_and_when(self):
        transition_request(self.request, RequestState.SUBMITTED, self.requester)
        transition_request(self.request, RequestState.APPROVED, self.committee)
        self.assertIsNotNone(self.request.approved_at)
        transition_request(self.request, RequestState.ACCEPTED, self.requester)
        self.assertIsNotNone(self.request.accepted_at)
        self.assertEqual(self.request.accepted_by, self.requester)


class RequestAccessTests(TestCase):
    def setUp(self):
        self.owner = make_requester('owner')
        self.other = make_requester('other')
        self.committee = make_committee('committee')
        self.request = TravelRequest.objects.create(
            user=self.owner, event=make_event(), description='Talk',
        )

    def test_owner_can_view(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 200,
        )

    def test_committee_can_view(self):
        self.client.force_login(self.committee)
        self.assertEqual(
            self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 200,
        )

    def test_other_requester_cannot_view(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 403,
        )

    def test_only_owner_can_submit(self):
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.post(reverse('request_submit', args=[self.request.pk])).status_code, 403,
        )

    def test_requester_cannot_approve(self):
        self.request.state = RequestState.SUBMITTED
        self.request.save()
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(reverse('request_approve', args=[self.request.pk])).status_code, 403,
        )
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED)

    def test_committee_approves_with_amounts(self):
        self.request.state = RequestState.SUBMITTED
        self.request.save()
        expense = Expense.objects.create(
            request=self.request, category='transportation',
            description='Flight', estimated_amount=600,
        )
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[self.request.pk]), {
            'expense_id': [str(expense.pk)],
            f'approved_amount_{expense.pk}': '400',
            f'approved_currency_{expense.pk}': 'EUR',
        })
        self.request.refresh_from_db()
        expense.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.APPROVED)
        self.assertEqual(expense.approved_amount, 400)
