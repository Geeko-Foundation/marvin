from datetime import date
from decimal import Decimal

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from profile.groups import GROUP_FINANCE, GROUP_REQUESTER
from tsp.models import (
    Event,
    Expense,
    ExpenseCategory,
    RequestState,
    TravelRequest,
)
from tsp.tests import make_event, make_user


class MixedCurrencyTests(TestCase):
    """A request may mix currencies, so nothing may add them up blindly."""

    def setUp(self):
        self.owner = make_user('curowner', [GROUP_REQUESTER])
        self.event = Event.objects.create(
            name='Conf', location='Sydney',
            start_date=date(2026, 1, 13), end_date=date(2026, 1, 17),
        )
        self.request = TravelRequest.objects.create(
            user=self.owner, event=self.event, state=RequestState.SUBMITTED_FOR_PAYMENT,
            description='Trip',
        )

    def _pays_into(self, currency):
        self.owner.profile.banking_currency = currency
        self.owner.profile.wise_account_id = 7
        self.owner.profile.save()

    def _expense(self, amount, currency):
        return Expense.objects.create(
            request=self.request, category='flight', description='plane',
            estimated_amount=amount, estimated_currency=currency,
            approved_amount=amount, approved_currency=currency,
        )

    def test_a_usd_line_is_not_reported_as_euros(self):
        # One 3.00 USD line once showed a total of "3.00 EUR".
        self._expense(Decimal('3.00'), 'USD')
        self.assertEqual(self.request.totals_approved(), [('USD', Decimal('3.00'))])

    def test_each_currency_is_totalled_separately(self):
        self._expense(Decimal('250.00'), 'EUR')
        self._expense(Decimal('3.00'), 'USD')
        self._expense(Decimal('150.00'), 'EUR')
        self.assertEqual(
            self.request.totals_approved(),
            [('EUR', Decimal('400.00')), ('USD', Decimal('3.00'))],
        )
        self.assertTrue(self.request.has_mixed_currencies())
        self.assertIsNone(self.request.payment_currency())

    def test_one_currency_is_payable_as_a_single_transfer(self):
        self._expense(Decimal('250.00'), 'EUR')
        self._expense(Decimal('150.00'), 'EUR')
        self.assertFalse(self.request.has_mixed_currencies())
        self.assertEqual(self.request.payment_currency(), 'EUR')

    def test_the_page_shows_the_real_currency(self):
        self._expense(Decimal('3.00'), 'USD')
        self.client.force_login(self.owner)
        html = self.client.get(reverse('request_detail', args=[self.request.pk])).content.decode()
        self.assertIn('3.00 USD', html)
        self.assertNotIn('3.00 EUR', html)

    def test_a_mixed_request_is_paid_in_the_payees_currency_at_the_average_rate(self):
        from tsp.models import ExchangeRate
        from tsp.payables import TravelRequestPayableAdapter

        # One euro buys 1.25 USD on one day and 1.00 USD on another: 1 USD averages 0.9 EUR.
        ExchangeRate.objects.create(date=date(2026, 1, 5), currency='USD', rate=Decimal('1.25'))
        ExchangeRate.objects.create(date=date(2026, 1, 6), currency='USD', rate=Decimal('1.00'))
        self._pays_into('EUR')
        self._expense(Decimal('250.00'), 'EUR')
        self._expense(Decimal('100.00'), 'USD')
        payment = TravelRequestPayableAdapter().build_payment_request(self.request)
        self.assertEqual((payment.amount, payment.currency), (Decimal('340.00'), 'EUR'))
        self.assertEqual(payment.metadata['converted_from']['USD']['rate'], '0.90000000')

    def test_a_request_in_one_currency_is_paid_in_the_bank_accounts_currency(self):
        from tsp.models import ExchangeRate
        from tsp.payables import TravelRequestPayableAdapter

        ExchangeRate.objects.create(date=date(2026, 1, 5), currency='IDR', rate=Decimal('20000'))
        self._pays_into('EUR')
        self._expense(Decimal('1000000.00'), 'IDR')
        payment = TravelRequestPayableAdapter().build_payment_request(self.request)
        # The gateway turns it into euros at Wise's rate; the page shows the estimate.
        self.assertEqual((payment.currency, payment.metadata['account_currency']), ('IDR', 'EUR'))
        self.client.force_login(self.owner)
        html = self.client.get(reverse('request_detail', args=[self.request.pk])).content.decode()
        self.assertIn('Paid to the bank account as about <strong>50.00 EUR</strong>', html)

    def test_the_bank_accounts_currency_needs_no_conversion(self):
        from tsp.payables import TravelRequestPayableAdapter

        self._pays_into('USD')
        self._expense(Decimal('3.00'), 'USD')
        payment = TravelRequestPayableAdapter().build_payment_request(self.request)
        self.assertEqual((payment.amount, payment.currency), (Decimal('3.00'), 'USD'))
        self.assertNotIn('converted_from', payment.metadata)
        self.assertIsNone(self.request.payout_preview())

    def test_a_mixed_request_without_rates_is_not_paid_with_a_guess(self):
        from tsp.payables import TravelRequestPayableAdapter

        self._expense(Decimal('250.00'), 'EUR')
        self._expense(Decimal('3.00'), 'USD')
        with self.assertRaises(ValueError) as caught:
            TravelRequestPayableAdapter().build_payment_request(self.request)
        self.assertIn('exchange rate', str(caught.exception))

    def test_a_single_currency_request_still_pays(self):
        from tsp.payables import TravelRequestPayableAdapter

        self._expense(Decimal('250.00'), 'USD')
        payment = TravelRequestPayableAdapter().build_payment_request(self.request)
        self.assertEqual(payment.amount, Decimal('250.00'))
        self.assertEqual(payment.currency, 'USD')

    def test_the_report_totals_are_grouped_by_currency(self):
        self._expense(Decimal('250.00'), 'EUR')
        self._expense(Decimal('3.00'), 'USD')
        staff = make_user('curfinance', [GROUP_FINANCE])
        self.client.force_login(staff)
        response = self.client.get(reverse('reports'))
        self.assertEqual(
            list(response.context['total_approved']),
            [('EUR', Decimal('250.00')), ('USD', Decimal('3.00'))],
        )

    def test_the_csv_export_carries_the_currency(self):
        self._expense(Decimal('250.00'), 'EUR')
        self._expense(Decimal('3.00'), 'USD')
        staff = make_user('curfinance2', [GROUP_FINANCE])
        self.client.force_login(staff)
        body = self.client.get(reverse('reports'), {'format': 'csv'}).content.decode()
        self.assertIn('250.00 EUR; 3.00 USD', body)
        self.assertIn('Paid Currency', body)

    def test_reports_handle_missing_and_blank_approved_currencies(self):
        self._expense(Decimal('250.00'), 'EUR')
        for missing in (None, ''):
            expense = self._expense(Decimal('10.00'), 'USD')
            expense.approved_currency = missing
            expense.save()
        self.client.force_login(make_user('legacyfinance', [GROUP_FINANCE]))
        response = self.client.get(reverse('reports'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['total_approved']),
                         [('EUR', Decimal('250.00')), ('USD', Decimal('20.00'))])
        csv = self.client.get(reverse('reports'), {'format': 'csv'})
        self.assertEqual(csv.status_code, 200)
        self.assertIn('250.00 EUR; 20.00 USD', csv.content.decode())


class ExpenseCategoryTests(TestCase):
    """The categories follow the travel policy, which pays for travel only."""

    def test_only_what_the_policy_covers_is_offered(self):
        self.assertEqual(
            [value for value, _ in ExpenseCategory.choices],
            ['flight', 'train', 'lodging', 'other'])

    def test_a_retired_line_keeps_what_it_was_for(self):
        from importlib import import_module

        migration = import_module(
            'tsp.migrations.0012_expense_categories_follow_travel_policy')
        request = TravelRequest.objects.create(
            user=make_user('retiredcat', [GROUP_REQUESTER]), event=make_event())
        lines = {
            'transportation': 'Flight to Nuremberg',
            'meals': 'Dinner',
            'visa': 'Schengen application',
            'registration': 'Conference ticket',
        }
        created = {
            value: Expense.objects.create(
                request=request, category=value, description=description,
                estimated_amount=20, estimated_currency='EUR')
            for value, description in lines.items()
        }

        migration.move_retired_categories(apps, None)

        for expense in created.values():
            expense.refresh_from_db()
        self.assertEqual(created['transportation'].description,
                         'Transportation: Flight to Nuremberg')
        self.assertEqual(created['meals'].description, 'Meals: Dinner')
        self.assertEqual(created['visa'].description, 'Visa Fees: Schengen application')
        self.assertEqual(created['registration'].description,
                         'Registration: Conference ticket')
        self.assertEqual({e.category for e in created.values()}, {'other'})

    def test_lodging_is_left_alone(self):
        from importlib import import_module

        migration = import_module(
            'tsp.migrations.0012_expense_categories_follow_travel_policy')
        request = TravelRequest.objects.create(
            user=make_user('lodgingkeeper', [GROUP_REQUESTER]), event=make_event())
        stay = Expense.objects.create(
            request=request, category='lodging', description='Hostel',
            estimated_amount=60, estimated_currency='EUR')

        migration.move_retired_categories(apps, None)

        stay.refresh_from_db()
        self.assertEqual(stay.category, 'lodging')
        self.assertEqual(stay.description, 'Hostel')

