from django.test import TestCase
from django.urls import reverse

from tsp.models import Expense, RequestState, StateHistory, TravelRequest
from tsp.tests import make_committee, make_event, make_requester


class BatchReviewTests(TestCase):
    def setUp(self):
        self.committee = make_committee('batchcommittee')
        self.requester = make_requester('batchrequester')
        self.event = make_event()
        self.requests = []
        for i in range(3):
            travel_request = TravelRequest.objects.create(
                user=self.requester, event=self.event,
                description=f'Talk {i}', state=RequestState.SUBMITTED,
            )
            Expense.objects.create(
                request=travel_request, category='transportation',
                description='Flight', estimated_amount=500 + i, estimated_currency='EUR',
            )
            self.requests.append(travel_request)

    def post(self, user, data):
        self.client.force_login(user)
        return self.client.post(reverse('request_batch_review'), data)

    def test_approving_in_bulk_approves_the_amount_requested(self):
        self.post(self.committee, {
            'request_ids': [r.pk for r in self.requests], 'action': 'approve',
        })
        for travel_request in self.requests:
            travel_request.refresh_from_db()
            self.assertEqual(travel_request.state, RequestState.APPROVED)
            expense = travel_request.expenses.get()
            self.assertEqual(expense.approved_amount, expense.estimated_amount)
            self.assertEqual(expense.approved_currency, 'EUR')
            # the whole point: finance must not receive a request worth nothing
            self.assertGreater(travel_request.total_approved(), 0)

    def test_an_amount_already_set_is_left_alone(self):
        expense = self.requests[0].expenses.get()
        expense.approved_amount = 100
        expense.approved_currency = 'EUR'
        expense.save()
        self.post(self.committee, {'request_ids': [self.requests[0].pk], 'action': 'approve'})
        expense.refresh_from_db()
        self.assertEqual(expense.approved_amount, 100)

    def test_rejecting_in_bulk_records_the_note(self):
        self.post(self.committee, {
            'request_ids': [self.requests[0].pk], 'action': 'reject', 'notes': 'Out of budget',
        })
        self.requests[0].refresh_from_db()
        self.assertEqual(self.requests[0].state, RequestState.REJECTED)
        history = StateHistory.objects.filter(request=self.requests[0]).latest('id')
        self.assertEqual(history.notes, 'Out of budget')

    def test_rejecting_without_a_note_rejects_nothing(self):
        self.post(self.committee, {'request_ids': [self.requests[0].pk], 'action': 'reject'})
        self.requests[0].refresh_from_db()
        self.assertEqual(self.requests[0].state, RequestState.SUBMITTED)

    def test_requests_that_already_moved_on_are_skipped(self):
        self.requests[0].state = RequestState.APPROVED
        self.requests[0].save()
        self.post(self.committee, {
            'request_ids': [r.pk for r in self.requests], 'action': 'reject', 'notes': 'No',
        })
        states = [TravelRequest.objects.get(pk=r.pk).state for r in self.requests]
        self.assertEqual(states[0], RequestState.APPROVED)
        self.assertEqual(states[1:], [RequestState.REJECTED, RequestState.REJECTED])

    def test_a_requester_cannot_batch_review(self):
        response = self.post(self.requester, {
            'request_ids': [self.requests[0].pk], 'action': 'approve',
        })
        self.assertEqual(response.status_code, 403)
        self.requests[0].refresh_from_db()
        self.assertEqual(self.requests[0].state, RequestState.SUBMITTED)
