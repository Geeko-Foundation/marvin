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
        self.propose(make_user('traveller', [GROUP_REQUESTER]), budget='99999', accepting_requests='on',
                     suggested_budget='5000', budget_currency='USD')
        event = Event.objects.get(name='Asia Summit')
        self.assertFalse(event.accepting_requests)
        self.assertIsNone(event.budget)
        self.assertEqual((event.suggested_budget, event.budget_currency), (Decimal('5000'), 'USD'))

    def test_finance_can_propose_too(self):
        response = self.propose(make_user('counter', [GROUP_FINANCE]))
        self.assertRedirects(response, reverse('event_list'), fetch_redirect_response=False)

    def test_the_committee_still_creates_an_open_event(self):
        from tsp.models import Event
        self.propose(make_user('reviewer', [GROUP_TSP]), accepting_requests='on', approval_percent='100',
                     budget_currency='EUR')
        self.assertTrue(Event.objects.get(name='Asia Summit').accepting_requests)


class OrganiserTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.organiser = make_user('organiser', [GROUP_REQUESTER])
        self.request = make_request(self.owner, RequestState.SUBMITTED)
        self.event = self.request.event
        self.event.organizers.add(self.organiser)
        self.client.force_login(self.organiser)

    def approve(self, travel_request, amount='500'):
        expense = travel_request.expenses.get()
        self.client.post(reverse('request_approve', args=[travel_request.pk]), {
            'expense_id': expense.pk, f'approved_amount_{expense.pk}': amount,
            f'approved_currency_{expense.pk}': 'EUR', 'approval_percent': '100',
        })
        travel_request.refresh_from_db()
        return travel_request.state

    def test_an_organiser_reviews_their_events_requests(self):
        response = self.client.get(reverse('request_detail', args=[self.request.pk]))
        self.assertContains(response, 'Organiser review')
        self.assertEqual(self.approve(self.request), RequestState.APPROVED)

    def test_an_organiser_cannot_touch_another_event(self):
        other = make_request(self.owner, RequestState.SUBMITTED)
        other.event.organizers.clear()
        self.assertEqual(self.client.get(reverse('request_detail', args=[other.pk])).status_code, 403)
        self.assertEqual(
            self.client.post(reverse('request_approve', args=[other.pk])).status_code, 403,
        )

    def test_an_organiser_stays_within_the_budget(self):
        self.event.budget, self.event.budget_currency = Decimal('100'), 'EUR'
        self.event.save()
        self.assertEqual(self.approve(self.request), RequestState.SUBMITTED)

    def test_the_role_ends_90_days_after_the_event(self):
        from datetime import date, timedelta
        self.event.start_date = self.event.end_date = date.today() - timedelta(days=91)
        self.event.save()
        self.assertEqual(self.client.get(reverse('request_detail', args=[self.request.pk])).status_code, 403)

    def test_an_organiser_cannot_edit_the_event_or_pay(self):
        self.assertEqual(self.client.get(reverse('event_edit', args=[self.event.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('payments:dashboard')).status_code, 403)

    def test_the_dashboard_lists_requests_to_review(self):
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'Events you organise')
        self.assertContains(response, self.request.reference)

    def test_the_committee_picks_organisers_on_the_event(self):
        self.client.force_login(make_user('chair', [GROUP_TSP]))
        event = self.event
        self.client.post(reverse('event_edit', args=[event.pk]), {
            'name': event.name, 'location': event.location, 'start_date': event.start_date,
            'end_date': event.end_date, 'approval_percent': '100', 'budget_currency': 'EUR',
            'accepting_requests': 'on', 'organizers': [self.owner.pk],
        })
        self.assertEqual(list(event.organizers.all()), [self.owner])


class ReimbursementFlowTests(TestCase):

    def setUp(self):
        self.owner = make_user('traveller', [GROUP_REQUESTER])
        self.client.force_login(self.owner)

    def test_accepting_goes_home_with_a_friendly_message(self):
        travel_request = make_request(self.owner, RequestState.APPROVED)
        from tsp.forms import AcceptForm
        response = self.client.post(reverse('request_accept', args=[travel_request.pk]),
                                    {name: 'on' for name in AcceptForm.base_fields}, follow=True)
        travel_request.refresh_from_db()
        self.assertEqual(travel_request.state, RequestState.ACCEPTED)
        self.assertRedirects(response, reverse('dashboard'))
        self.assertContains(response, 'Enjoy the event')

    def test_documents_come_after_the_actual_costs(self):
        travel_request = make_request(self.owner, RequestState.ACCEPTED)
        page = self.client.get(reverse('request_detail', args=[travel_request.pk]))
        self.assertContains(page, 'Ask for reimbursement')
        self.assertContains(page, 'once your actual costs are in')
        expense = travel_request.expenses.get()
        expense.actual_amount, expense.actual_currency = Decimal('480'), 'EUR'
        expense.save()
        page = self.client.get(reverse('request_detail', args=[travel_request.pk]))
        self.assertContains(page, 'Print signed form')

    def test_the_signed_form_shows_actual_costs(self):
        travel_request = make_request(self.owner, RequestState.ACCEPTED, actual='480')
        page = self.client.get(reverse('request_form', args=[travel_request.pk]))
        self.assertContains(page, 'Actual Expenses')
        self.assertContains(page, '480.00 EUR')
