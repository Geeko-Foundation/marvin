"""Changes asked for in the 19 November 2026 walkthrough."""

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from payments.models import PaymentRecord
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP
from tsp.models import RequestState
from tsp.test_advances import make_request
from tsp.tests import make_user


class FinanceCancelTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.finance = make_user('counter', [GROUP_FINANCE])
        self.client.force_login(self.finance)

    def cancel(self, travel_request):
        self.client.post(reverse('request_cancel', args=[travel_request.pk]))
        travel_request.refresh_from_db()
        return travel_request.state

    def test_finance_can_cancel_a_request_waiting_for_payment(self):
        travel_request = make_request(self.owner, RequestState.SUBMITTED_FOR_PAYMENT)
        self.assertEqual(self.cancel(travel_request), RequestState.CANCELLED)

    def test_finance_cannot_cancel_before_it_reaches_them(self):
        travel_request = make_request(self.owner, RequestState.ACCEPTED)
        response = self.client.post(reverse('request_cancel', args=[travel_request.pk]))
        self.assertEqual(response.status_code, 403)

    def test_a_payment_under_way_stops_the_cancel(self):
        travel_request = make_request(self.owner, RequestState.SUBMITTED_FOR_PAYMENT)
        PaymentRecord.objects.create(
            content_type=ContentType.objects.get_for_model(travel_request), object_id=travel_request.pk,
            gateway_id='wise-api', amount=Decimal('500'), currency='EUR', recipient_name='T',
            recipient_email='t@example.org', reference=travel_request.reference, status='processing',
        )
        self.assertEqual(self.cancel(travel_request), RequestState.SUBMITTED_FOR_PAYMENT)


class ReviewButtonTests(TestCase):

    def test_a_request_under_review_offers_reject_but_not_cancel(self):
        travel_request = make_request(make_user('traveller', [GROUP_REQUESTER]), RequestState.SUBMITTED)
        self.client.force_login(make_user('reviewer', [GROUP_TSP]))
        response = self.client.get(reverse('request_detail', args=[travel_request.pk]))
        self.assertContains(response, 'Reject Request')
        self.assertNotContains(response, 'Cancel Request')


class BulkApproveTests(TestCase):

    def test_approving_everything_selected_says_so(self):
        owner = make_user('traveller', [GROUP_REQUESTER])
        ids = [make_request(owner, RequestState.SUBMITTED).pk for _ in range(2)]
        self.client.force_login(make_user('reviewer', [GROUP_TSP]))
        response = self.client.post(
            reverse('request_batch_review'), {'request_ids': ids, 'action': 'approve'}, follow=True,
        )
        self.assertContains(response, 'All selected requests approved.')


class FinanceDashboardTests(TestCase):

    def test_recently_paid_links_to_the_request(self):
        travel_request = make_request(make_user('traveller', [GROUP_REQUESTER]), RequestState.PAID)
        self.client.force_login(make_user('counter', [GROUP_FINANCE]))
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, reverse('request_detail', args=[travel_request.pk]))


class EventProposalTests(TestCase):

    def propose(self, user, **extra):
        self.client.force_login(user)
        return self.client.post(reverse('event_create'), {
            'name': 'Asia Summit', 'location': 'Tokyo', 'start_date': '2027-08-01',
            'end_date': '2027-08-03', **extra,
        })

    def test_a_requester_proposes_an_event_that_stays_closed(self):
        from tsp.models import Event
        self.propose(make_user('traveller', [GROUP_REQUESTER]), budget='99999', accepting_requests='on')
        event = Event.objects.get(name='Asia Summit')
        self.assertFalse(event.accepting_requests)
        self.assertIsNone(event.budget)

    def test_finance_can_propose_too(self):
        response = self.propose(make_user('counter', [GROUP_FINANCE]))
        self.assertRedirects(response, reverse('event_list'), fetch_redirect_response=False)

    def test_the_committee_still_creates_an_open_event(self):
        from tsp.models import Event
        self.propose(make_user('reviewer', [GROUP_TSP]), accepting_requests='on', approval_percent='100',
                     budget_currency='EUR')
        self.assertTrue(Event.objects.get(name='Asia Summit').accepting_requests)
