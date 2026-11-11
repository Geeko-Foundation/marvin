from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from payments.models import PaymentRecord
from payments.services import PaymentService
from profile.groups import GROUP_FINANCE
from tsp.models import Document, Expense, RequestState, TravelRequest
from tsp.tests import make_committee, make_event, make_requester, make_user


def make_request(owner, state, amount='500', actual=None, currency='EUR'):
    travel_request = TravelRequest.objects.create(
        user=owner, event=make_event(), description='Talk', state=state,
    )
    Expense.objects.create(
        request=travel_request, category='flight', description='Train',
        estimated_amount=Decimal(amount), estimated_currency=currency,
        approved_amount=Decimal(amount) if state != RequestState.SUBMITTED else None,
        approved_currency=currency if state != RequestState.SUBMITTED else None,
        actual_amount=Decimal(actual) if actual else None,
        actual_currency=currency if actual else None,
    )
    return travel_request


def add_documents(travel_request):
    for kind in ('receipt', 'invoice', 'signed_form'):
        Document.objects.create(
            request=travel_request, kind=kind, file=f'uploads/{kind}.pdf',
            original_name=f'{kind}.pdf', mimetype='application/pdf', size=10,
            uploaded_by=travel_request.user,
        )


def pay(travel_request, user):
    return PaymentService().process_payment(travel_request.pk, 'manual', user)


class TakeBackTests(TestCase):
    def setUp(self):
        self.owner = make_requester('tbowner')
        self.request = make_request(self.owner, RequestState.SUBMITTED_FOR_PAYMENT, actual='400')

    def test_the_requester_can_take_it_back_and_resubmit(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('request_take_back', args=[self.request.pk]))
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.ACCEPTED)

        expense = self.request.expenses.get()
        self.client.post(reverse('request_actuals', args=[self.request.pk]), {
            'expenses-TOTAL_FORMS': '1', 'expenses-INITIAL_FORMS': '1',
            'expenses-MIN_NUM_FORMS': '0', 'expenses-MAX_NUM_FORMS': '1000',
            'expenses-0-id': str(expense.pk),
            'expenses-0-actual_amount': '350', 'expenses-0-actual_currency': 'EUR',
        })
        expense.refresh_from_db()
        self.assertEqual(expense.actual_amount, Decimal('350'))

        add_documents(self.request)
        self.client.post(reverse('request_submit_for_payment', args=[self.request.pk]))
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_not_once_finance_has_started_paying(self):
        PaymentRecord.objects.create(
            content_object=self.request, gateway_id='wise-api', amount=400, currency='EUR',
            recipient_name='x', recipient_email='x@example.org', reference='TSP', status='pending',
        )
        self.client.force_login(self.owner)
        self.client.post(reverse('request_take_back', args=[self.request.pk]))
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_only_the_owner_can_take_it_back(self):
        self.client.force_login(make_requester('tbother'))
        response = self.client.post(reverse('request_take_back', args=[self.request.pk]))
        self.assertEqual(response.status_code, 403)


class ChangeAmountsTests(TestCase):
    def setUp(self):
        self.owner = make_requester('caowner')
        self.request = make_request(self.owner, RequestState.APPROVED)
        self.request.advance_amount = Decimal('100')
        self.request.advance_currency = 'EUR'
        self.request.save()

    def _edit(self, amount):
        expense = self.request.expenses.get()
        self.client.force_login(self.owner)
        return self.client.post(reverse('request_edit', args=[self.request.pk]), {
            'event': self.request.event.pk, 'description': 'Talk',
            'expenses-TOTAL_FORMS': '1', 'expenses-INITIAL_FORMS': '1',
            'expenses-MIN_NUM_FORMS': '0', 'expenses-MAX_NUM_FORMS': '1000',
            'expenses-0-id': str(expense.pk), 'expenses-0-category': 'flight',
            'expenses-0-description': 'Train', 'expenses-0-estimated_amount': amount,
            'expenses-0-estimated_currency': 'EUR',
        })

    def test_changing_amounts_after_approval_goes_back_for_review(self):
        self._edit('650')
        self.request.refresh_from_db()
        expense = self.request.expenses.get()
        self.assertEqual(self.request.state, RequestState.SUBMITTED)
        self.assertEqual(expense.estimated_amount, Decimal('650'))
        self.assertIsNone(expense.approved_amount)
        self.assertIsNone(self.request.advance_amount)

    def test_not_once_an_advance_has_been_paid(self):
        self.request.state = RequestState.ACCEPTED
        self.request.save()
        PaymentRecord.objects.create(
            content_object=self.request, gateway_id='manual', amount=100, currency='EUR',
            recipient_name='x', recipient_email='x@example.org', reference='TSP',
            purpose='advance', status='completed',
        )
        self._edit('650')
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.ACCEPTED)
        self.assertEqual(self.request.expenses.get().estimated_amount, Decimal('500'))


class AdvanceTests(TestCase):
    def setUp(self):
        self.owner = make_requester('advowner')
        self.committee = make_committee('advcommittee')
        self.finance = make_user('advfinance', [GROUP_FINANCE])

    def _approve_with_advance(self, amount, currency='EUR'):
        travel_request = make_request(self.owner, RequestState.SUBMITTED)
        expense = travel_request.expenses.get()
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[travel_request.pk]), {
            'expense_id': [str(expense.pk)],
            f'approved_amount_{expense.pk}': '500', f'approved_currency_{expense.pk}': 'EUR',
            'advance_amount': amount, 'advance_currency': currency,
        })
        travel_request.refresh_from_db()
        return travel_request

    def _accept_and_request(self, travel_request):
        travel_request.state = RequestState.ACCEPTED
        travel_request.save()
        self.client.force_login(self.owner)
        self.client.post(reverse('request_advance', args=[travel_request.pk]))
        travel_request.refresh_from_db()

    def test_the_committee_sets_the_advance_when_approving(self):
        travel_request = self._approve_with_advance('200')
        self.assertEqual(travel_request.state, RequestState.APPROVED)
        self.assertEqual((travel_request.advance_amount, travel_request.advance_currency),
                         (Decimal('200'), 'EUR'))

    def test_an_advance_cannot_exceed_the_approval(self):
        travel_request = self._approve_with_advance('600')
        self.assertEqual(travel_request.state, RequestState.SUBMITTED)
        travel_request = self._approve_with_advance('100', 'USD')
        self.assertEqual(travel_request.state, RequestState.SUBMITTED)

    def test_without_an_advance_none_can_be_requested(self):
        travel_request = make_request(self.owner, RequestState.ACCEPTED)
        self._accept_and_request(travel_request)
        self.assertIsNone(travel_request.advance_requested_at)

    def test_a_requested_advance_is_paid_before_the_event_and_taken_off_later(self):
        travel_request = self._approve_with_advance('200')
        self._accept_and_request(travel_request)
        self.assertTrue(PaymentService().get_queue().filter(pk=travel_request.pk).exists())

        advance = pay(travel_request, self.finance)
        travel_request.refresh_from_db()
        self.assertEqual((advance.purpose, advance.amount, advance.reference),
                         ('advance', Decimal('200.00'), f'{travel_request.reference}-ADVANCE'))
        self.assertEqual(travel_request.state, RequestState.ACCEPTED)
        self.assertFalse(PaymentService().get_queue().filter(pk=travel_request.pk).exists())

        expense = travel_request.expenses.get()
        expense.actual_amount, expense.actual_currency = Decimal('450'), 'EUR'
        expense.save()
        travel_request.state = RequestState.SUBMITTED_FOR_PAYMENT
        travel_request.save()

        final = pay(travel_request, self.finance)
        travel_request.refresh_from_db()
        self.assertEqual((final.purpose, final.amount), ('payment', Decimal('250.00')))
        self.assertEqual(travel_request.state, RequestState.PAID)

    def test_an_advance_larger_than_the_costs_is_owed_back_and_blocks_new_requests(self):
        travel_request = self._approve_with_advance('400')
        self._accept_and_request(travel_request)
        pay(travel_request, self.finance)
        expense = travel_request.expenses.get()
        expense.actual_amount, expense.actual_currency = Decimal('300'), 'EUR'
        expense.save()
        travel_request.state = RequestState.SUBMITTED_FOR_PAYMENT
        travel_request.save()

        self.assertEqual(travel_request.amount_owed_back(), ('EUR', Decimal('100.00')))
        self.assertTrue(travel_request.advance_covers_costs())

        draft = make_request(self.owner, RequestState.DRAFT)
        self.client.force_login(self.owner)
        self.client.post(reverse('request_submit', args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.state, RequestState.DRAFT)
        self.assertRedirects(
            self.client.get(reverse('request_create')),
            reverse('request_detail', args=[travel_request.pk]),
        )

        self.client.force_login(self.finance)
        self.client.post(reverse('request_settle', args=[travel_request.pk]), {'action': 'close'})
        self.client.post(reverse('request_settle', args=[travel_request.pk]), {'action': 'refund'})
        travel_request.refresh_from_db()
        self.assertEqual(travel_request.state, RequestState.PAID)
        self.assertIsNone(travel_request.amount_owed_back())

        self.client.force_login(self.owner)
        self.client.post(reverse('request_submit', args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.state, RequestState.SUBMITTED)

    def test_not_attending_after_an_advance_owes_all_of_it(self):
        travel_request = self._approve_with_advance('200')
        self._accept_and_request(travel_request)
        pay(travel_request, self.finance)
        self.client.force_login(self.owner)
        self.client.post(reverse('request_cancel', args=[travel_request.pk]))
        travel_request.refresh_from_db()
        self.assertEqual(travel_request.state, RequestState.CANCELLED)
        self.assertEqual(travel_request.amount_owed_back(), ('EUR', Decimal('200.00')))

    def test_only_finance_records_refunds(self):
        travel_request = make_request(self.owner, RequestState.PAID)
        self.client.force_login(self.owner)
        response = self.client.post(reverse('request_settle', args=[travel_request.pk]), {'action': 'refund'})
        self.assertEqual(response.status_code, 403)

    def test_an_unpaid_advance_waits_before_submitting_for_payment(self):
        travel_request = self._approve_with_advance('200')
        self._accept_and_request(travel_request)
        PaymentRecord.objects.create(
            content_object=travel_request, gateway_id='wise-api', amount=200, currency='EUR',
            recipient_name='x', recipient_email='x@example.org', reference='TSP',
            purpose='advance', status='pending',
        )
        expense = travel_request.expenses.get()
        expense.actual_amount, expense.actual_currency = Decimal('450'), 'EUR'
        expense.save()
        add_documents(travel_request)
        self.client.force_login(self.owner)
        self.client.post(reverse('request_submit_for_payment', args=[travel_request.pk]))
        travel_request.refresh_from_db()
        self.assertEqual(travel_request.state, RequestState.ACCEPTED)
