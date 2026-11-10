from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tsp.models import ExchangeRate, Expense, RequestState, TravelRequest
from tsp.services import exchange_rates
from tsp.tests import make_committee, make_event, make_requester


def store_rates(days_back, **rates_per_eur):
    """The same euro rates for each of the last ``days_back`` days, up to today."""
    today = timezone.localdate()
    for offset in range(days_back):
        for currency, rate in rates_per_eur.items():
            ExchangeRate.objects.create(
                date=today - timedelta(days=offset), currency=currency, rate=Decimal(rate),
            )


def fake_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class RateMathTests(TestCase):
    def test_a_pair_is_the_ratio_of_two_euro_rates(self):
        store_rates(1, USD='1.10', INR='99.00')
        rates = exchange_rates.summary('USD', 'INR')
        self.assertEqual(rates.current, Decimal('90'))

    def test_euro_needs_no_stored_rate(self):
        store_rates(1, INR='100.00')
        self.assertEqual(exchange_rates.summary('INR', 'EUR').current, Decimal('0.01'))

    def test_the_average_covers_the_window_and_smooths_swings(self):
        today = timezone.localdate()
        ExchangeRate.objects.create(date=today, currency='INR', rate=Decimal('110'))
        ExchangeRate.objects.create(date=today - timedelta(days=30), currency='INR', rate=Decimal('90'))
        # Outside the 90 days, so it must not count.
        ExchangeRate.objects.create(date=today - timedelta(days=200), currency='INR', rate=Decimal('10'))
        rates = exchange_rates.summary('EUR', 'INR')
        self.assertEqual(rates.average, Decimal('100'))
        self.assertEqual(rates.current, Decimal('110'))
        self.assertEqual((rates.low, rates.high), (Decimal('90'), Decimal('110')))

    def test_no_rates_means_no_summary(self):
        self.assertIsNone(exchange_rates.summary('USD', 'INR'))
        self.assertIsNone(exchange_rates.summary('USD', 'USD'))

    def test_the_window_stops_at_an_event_that_has_started(self):
        event = make_event()
        event.start_date = timezone.localdate() - timedelta(days=10)
        self.assertEqual(exchange_rates.window_end_for(event), event.start_date)
        event.start_date = timezone.localdate() + timedelta(days=100)
        self.assertEqual(exchange_rates.window_end_for(event), timezone.localdate())


class FetchTests(TestCase):
    def setUp(self):
        cache.clear()

    @mock.patch('tsp.services.exchange_rates.requests.get')
    def test_past_rates_are_stored_per_day(self, get):
        get.return_value = fake_response({'rates': {
            '2026-10-01': {'USD': 1.1, 'INR': 95.5},
            '2026-10-02': {'USD': 1.2, 'INR': 96.5},
        }})
        stored = exchange_rates.refresh(date(2026, 10, 1), date(2026, 10, 2))
        self.assertEqual(stored, 4)
        rate = ExchangeRate.objects.get(date=date(2026, 10, 2), currency='INR')
        self.assertEqual((rate.rate, rate.source), (Decimal('96.5'), 'ecb'))

    @mock.patch('tsp.services.exchange_rates.requests.get')
    def test_a_failed_fetch_stores_nothing(self, get):
        get.side_effect = requests.ConnectionError('down')
        self.assertEqual(exchange_rates.refresh(date(2026, 10, 1), date(2026, 10, 2)), 0)

    @override_settings(EXCHANGE_RATE_API_KEY='')
    @mock.patch('tsp.services.exchange_rates.requests.get')
    def test_today_needs_a_key(self, get):
        self.assertEqual(exchange_rates.refresh_latest(), 0)
        get.assert_not_called()

    @override_settings(EXCHANGE_RATE_API_KEY='secret-key')
    @mock.patch('tsp.services.exchange_rates.requests.get')
    def test_today_comes_from_exchangerate_api(self, get):
        get.return_value = fake_response({
            'result': 'success',
            'time_last_update_unix': 1793318401,  # 30 Oct 2026, 00:00:01 UTC
            'conversion_rates': {'EUR': 1, 'INR': 97.25, 'XAF': 655.9},
        })
        self.assertEqual(exchange_rates.refresh_latest(), 1)
        self.assertIn('/secret-key/latest/EUR', get.call_args.args[0])
        rate = ExchangeRate.objects.get(currency='INR')
        self.assertEqual((rate.date, rate.source), (date(2026, 10, 30), 'exchangerate-api'))

    @override_settings(EXCHANGE_RATE_API_KEY='secret-key')
    @mock.patch('tsp.services.exchange_rates.requests.get')
    def test_the_key_never_reaches_the_log(self, get):
        get.side_effect = requests.ConnectionError('https://v6.example/secret-key/latest/EUR')
        with self.assertLogs('tsp.services.exchange_rates', 'WARNING') as logs:
            exchange_rates.refresh_latest()
        self.assertNotIn('secret-key', ''.join(logs.output))

    @override_settings(EXCHANGE_RATES_FETCH=True, EXCHANGE_RATE_API_KEY='')
    @mock.patch('tsp.services.exchange_rates.requests.get')
    def test_current_rates_are_not_fetched_again(self, get):
        store_rates(1, INR='95')
        ExchangeRate.objects.update(source='ecb')
        exchange_rates.ensure_recent()
        exchange_rates.ensure_recent()
        get.assert_not_called()


class ApprovalConversionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_requester('fxowner')
        self.committee = make_committee('fxcommittee')
        self.request = TravelRequest.objects.create(
            user=self.owner, event=make_event(), description='Talk', state=RequestState.SUBMITTED,
        )
        self.expense = Expense.objects.create(
            request=self.request, category='transportation', description='Train',
            estimated_amount=Decimal('9000'), estimated_currency='INR',
        )

    def _approve(self, amount, currency):
        self.client.force_login(self.committee)
        self.client.post(reverse('request_approve', args=[self.request.pk]), {
            'expense_id': [str(self.expense.pk)],
            f'approved_amount_{self.expense.pk}': amount,
            f'approved_currency_{self.expense.pk}': currency,
        })
        self.expense.refresh_from_db()

    def test_the_form_offers_the_amount_already_converted(self):
        store_rates(90, USD='1.00', INR='90.00')
        self.client.force_login(self.committee)
        html = self.client.get(reverse('request_detail', args=[self.request.pk])).content.decode()
        self.assertIn('value="100.00"', html)
        self.assertIn('<option value="EUR" data-amount="100.00" selected>', html)
        self.assertIn('1 EUR = 90.0000 INR', html)

    def test_without_rates_the_line_stays_in_the_requested_currency(self):
        self.client.force_login(self.committee)
        html = self.client.get(reverse('request_detail', args=[self.request.pk])).content.decode()
        self.assertIn('<option value="INR" data-amount="9000.00" selected>', html)
        self.assertNotIn('value="EUR"', html)

    def test_approving_in_another_currency_locks_the_average_rate(self):
        today = timezone.localdate()
        ExchangeRate.objects.create(date=today, currency='USD', rate=Decimal('1'))
        ExchangeRate.objects.create(date=today, currency='INR', rate=Decimal('80'))
        ExchangeRate.objects.create(date=today - timedelta(days=5), currency='USD', rate=Decimal('1'))
        ExchangeRate.objects.create(date=today - timedelta(days=5), currency='INR', rate=Decimal('100'))
        self._approve('90', 'USD')
        self.assertEqual(self.expense.approved_amount, Decimal('90'))
        self.assertEqual(self.expense.approval_rate, Decimal('90'))
        self.assertEqual(self.expense.approval_rate_date, today)
        self.assertEqual(self.expense.approved_in_requested_currency(), Decimal('8100.00'))

    def test_the_locked_rate_does_not_move_with_the_market(self):
        store_rates(1, USD='1', INR='90')
        self._approve('90', 'USD')
        ExchangeRate.objects.filter(currency='INR').update(rate=Decimal('200'))
        self.assertEqual(self.expense.approved_in_requested_currency(), Decimal('8100.00'))

    def test_the_same_currency_needs_no_rate(self):
        self._approve('8000', 'INR')
        self.assertIsNone(self.expense.approval_rate)
        self.assertIsNone(self.expense.approved_in_requested_currency())

    def test_approval_still_works_without_rates(self):
        self._approve('90', 'USD')
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.APPROVED)
        self.assertIsNone(self.expense.approval_rate)

    def test_the_requester_sees_the_amount_in_their_currency(self):
        store_rates(1, USD='1', INR='90')
        self._approve('90', 'USD')
        self.client.force_login(self.owner)
        html = self.client.get(reverse('request_detail', args=[self.request.pk])).content.decode()
        self.assertIn('90.00 USD', html)
        self.assertIn('≈ 8,100.00 INR', html)
