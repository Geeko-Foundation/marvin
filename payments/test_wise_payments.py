from decimal import Decimal
from unittest import mock

import requests
from django.test import TestCase, override_settings

from payments.base import PaymentRequest
from payments.gateways.wise import WiseApiGateway, transaction_id_for
from payments.gateways.wise_api import WiseApiError
from payments.models import PaymentRecord
from payments.services import PaymentService
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER
from tsp.models import Expense, RequestState, TravelRequest
from tsp.tests import make_event, make_user


def payment(amount='18.00', currency='EUR', reference='TSP12'):
    return PaymentRequest(
        amount=Decimal(amount), currency=currency, recipient_name='Test',
        recipient_email='t@example.org', reference=reference,
        metadata={'wise_account_id': 704030890},
    )


def gateway_with(client):
    with mock.patch('payments.gateways.wise.get_wise_client', return_value=client):
        return WiseApiGateway()


def wise_client(**overrides):
    client = mock.Mock()
    client.create_quote.return_value = {'id': 'quote-1', 'rate': 1.19}
    client.create_transfer.return_value = {'id': 2149867990, 'status': 'incoming_payment_waiting'}
    client.fund_or_advance_transfer.return_value = {
        'transfer': {'id': 2149867990, 'status': 'outgoing_payment_sent'},
    }
    for name, value in overrides.items():
        getattr(client, name).side_effect = value
    return client


class TransactionIdTests(TestCase):
    """Wise treats a repeated id as the same transfer, so the id decides what a retry does."""

    @override_settings(SITE_BASE_URL='https://tsp.example.org')
    def test_the_same_payment_always_gets_the_same_id(self):
        self.assertEqual(transaction_id_for(payment(), 1), transaction_id_for(payment(), 1))
        self.assertEqual(transaction_id_for(payment('18'), 1), transaction_id_for(payment('18.00'), 1))

    @override_settings(SITE_BASE_URL='https://tsp.example.org')
    def test_anything_that_changes_the_money_gets_a_new_id(self):
        base = transaction_id_for(payment(), 1)
        self.assertNotEqual(base, transaction_id_for(payment(amount='19.00'), 1))
        self.assertNotEqual(base, transaction_id_for(payment(currency='GBP'), 1))
        self.assertNotEqual(base, transaction_id_for(payment(), 2))
        self.assertNotEqual(base, transaction_id_for(payment(reference='TSP12-ADVANCE'), 1))

    def test_two_sites_sharing_a_wise_account_do_not_collide(self):
        with override_settings(SITE_BASE_URL='https://a.example.org'):
            first = transaction_id_for(payment(), 1)
        with override_settings(SITE_BASE_URL='https://b.example.org'):
            second = transaction_id_for(payment(), 1)
        self.assertNotEqual(first, second)


class AccountCurrencyTests(TestCase):
    """The requester receives the whole approved sum; the foundation pays Wise's fee on top."""

    def test_the_approved_amounts_worth_arrives_in_the_account_currency(self):
        client = wise_client()
        client.get_rate.return_value = Decimal('104.245')
        request = payment('100.00', 'EUR')
        request.metadata['account_currency'] = 'INR'
        gateway_with(client).initiate_payment(request)
        client.get_rate.assert_called_once_with('EUR', 'INR')
        client.create_quote.assert_called_once_with(
            source_currency='EUR', target_currency='INR',
            target_amount=Decimal('10424.50'), target_account=704030890)

    def test_currencies_without_cents_are_rounded_to_whole_units(self):
        from payments.gateways.wise import converted
        client = mock.Mock()
        client.get_rate.return_value = Decimal('163.4567')
        self.assertEqual(converted(client, Decimal('10.00'), 'EUR', 'JPY'), Decimal('1635'))

    def test_a_matching_account_still_receives_the_exact_amount(self):
        client = wise_client()
        request = payment('100.00', 'EUR')
        request.metadata['account_currency'] = 'EUR'
        gateway_with(client).initiate_payment(request)
        self.assertEqual(client.create_quote.call_args.kwargs['target_amount'], Decimal('100.00'))


class UnclearOutcomeTests(TestCase):
    """Once a transfer exists, an error must never be recorded as a clean failure."""

    def test_the_transfer_is_created_with_the_stable_id(self):
        client = wise_client()
        gateway_with(client).initiate_payment(payment())
        sent = client.create_transfer.call_args.kwargs['customer_transaction_id']
        self.assertEqual(sent, transaction_id_for(payment(), 704030890))

    def test_a_network_error_after_the_transfer_exists_leaves_it_processing(self):
        client = wise_client(fund_or_advance_transfer=requests.ConnectionError('reset'))
        result = gateway_with(client).initiate_payment(payment())
        self.assertEqual(result.status, 'processing')
        self.assertEqual(result.external_id, '2149867990')

    def test_a_wise_error_after_the_transfer_exists_leaves_it_processing(self):
        client = wise_client(fund_or_advance_transfer=WiseApiError('SCA required', 403))
        result = gateway_with(client).initiate_payment(payment())
        self.assertEqual(result.status, 'processing')
        self.assertEqual(result.external_id, '2149867990')

    def test_a_network_error_before_any_transfer_is_a_failure_not_a_crash(self):
        client = wise_client(create_quote=requests.Timeout('slow'))
        result = gateway_with(client).initiate_payment(payment())
        self.assertEqual(result.status, 'failed')
        self.assertIsNone(result.external_id)

    def test_status_is_read_from_the_funding_step_not_fetched_again(self):
        client = wise_client()
        result = gateway_with(client).initiate_payment(payment())
        self.assertEqual(result.external_id, '2149867990')
        client.get_transfer.assert_not_called()


class NoSecondPaymentTests(TestCase):
    """A payment whose outcome is unclear blocks another attempt until it is settled."""

    def test_an_unclear_payment_cannot_be_paid_again(self):
        owner = make_user('unclearowner', [GROUP_REQUESTER])
        finance = make_user('unclearfinance', [GROUP_FINANCE])
        req = TravelRequest.objects.create(
            user=owner, event=make_event(), state=RequestState.SUBMITTED_FOR_PAYMENT, description='T')
        Expense.objects.create(
            request=req, category='flight', description='F', estimated_amount=100,
            estimated_currency='EUR', approved_amount=100, approved_currency='EUR',
            actual_amount=90, actual_currency='EUR')
        client = wise_client(fund_or_advance_transfer=requests.ConnectionError('reset'))
        gateway = gateway_with(client)
        service = PaymentService()
        with mock.patch('payments.services.get_gateway', return_value=gateway):
            record = service.process_payment(req.pk, 'wise-api', finance)
            self.assertEqual(record.status, 'processing')
            with self.assertRaises(ValueError):
                service.process_payment(req.pk, 'wise-api', finance)
        self.assertEqual(client.create_transfer.call_count, 1)
        self.assertEqual(PaymentRecord.objects.filter(object_id=req.pk).count(), 1)


class WiseClientNetworkTests(TestCase):
    """Every Wise call has a time limit, and a dead network reads as a Wise error."""

    def make_wise(self):
        from payments.gateways.wise_api import WiseApiClient
        return WiseApiClient(api_token='t', profile_id='1', sandbox=True)

    def test_every_call_is_sent_with_a_timeout(self):
        from payments.gateways.wise_api import TIMEOUT
        wise = self.make_wise()
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = {'id': 1, 'status': 'processing'}
        with mock.patch.object(wise._session, 'request', return_value=response) as sent:
            wise.get_transfer(1)
            wise.list_balances()
            wise.fund_transfer(1)
        self.assertEqual(sent.call_count, 3)
        for call in sent.call_args_list:
            self.assertEqual(call.kwargs['timeout'], TIMEOUT)

    def test_a_timeout_is_raised_as_a_wise_error(self):
        wise = self.make_wise()
        with mock.patch.object(wise._session, 'request', side_effect=requests.Timeout('slow')):
            with self.assertRaises(WiseApiError):
                wise.get_transfer(1)


class BatchExportTests(TestCase):
    """A request goes into one batch file, and is paid once."""

    def setUp(self):
        self.owner = make_user('batchexportowner', [GROUP_REQUESTER])
        self.finance = make_user('batchexportfinance', [GROUP_FINANCE])
        self.req = TravelRequest.objects.create(
            user=self.owner, event=make_event(), state=RequestState.SUBMITTED_FOR_PAYMENT, description='T')
        Expense.objects.create(
            request=self.req, category='flight', description='F', estimated_amount=100,
            estimated_currency='EUR', approved_amount=100, approved_currency='EUR',
            actual_amount=90, actual_currency='EUR')
        self.service = PaymentService()

    def test_a_name_stored_before_validation_cannot_run_as_a_formula(self):
        self.owner.first_name, self.owner.last_name = '=HYPERLINK("http://x")', ''
        self.owner.save()
        content, _, _ = self.service.export_batch('wise-csv', self.finance)
        self.assertNotIn('\n=HYPERLINK', content)
        self.assertIn("'=HYPERLINK", content)

    def test_the_file_converts_into_the_recipients_account_currency(self):
        self.owner.profile.banking_currency = 'INR'
        self.owner.profile.save()
        wise = mock.Mock()
        wise.get_rate.return_value = Decimal('100')
        with mock.patch('payments.gateways.wise.get_wise_client', return_value=wise):
            content, _, _ = self.service.export_batch('wise-csv', self.finance)
        # The INR amount is what arrives, so Wise's fee is added to the foundation's side.
        self.assertIn(',INR,9000.00,EUR,INR,BANK_TRANSFER', content)

    def test_the_file_uses_the_requests_own_reference(self):
        content, _, _ = self.service.export_batch('wise-csv', self.finance)
        self.assertIn(f',{self.req.reference},', content)
        self.assertNotIn(f'{self.req.reference}-', content)

    def test_exporting_again_leaves_out_what_is_already_in_a_file(self):
        self.service.export_batch('wise-csv', self.finance)
        with self.assertRaises(ValueError):
            self.service.export_batch('wise-csv', self.finance)
        self.assertEqual(PaymentRecord.objects.filter(object_id=self.req.pk, status='exported').count(), 1)

    def test_an_exported_request_cannot_also_be_paid_through_the_api(self):
        self.service.export_batch('wise-csv', self.finance)
        with self.assertRaises(ValueError) as raised:
            self.service.process_payment(self.req.pk, 'wise-api', self.finance)
        self.assertIn('batch file', str(raised.exception))

    def test_marking_an_exported_request_paid_still_works(self):
        self.service.export_batch('wise-csv', self.finance)
        self.service.process_payment(self.req.pk, 'wise-csv', self.finance)
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, RequestState.PAID)

    def test_a_request_never_exported_cannot_be_marked_paid_by_batch(self):
        with self.assertRaises(ValueError) as raised:
            self.service.process_payment(self.req.pk, 'wise-csv', self.finance)
        self.assertIn('not been exported', str(raised.exception))
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_an_export_is_not_shown_as_money_paid(self):
        self.service.export_batch('wise-csv', self.finance)
        self.assertIsNone(self.req.payment)


class PaymentMessageTests(TestCase):
    """Finance is told what happened to the money, not just that the button worked."""

    def setUp(self):
        from payments.base import PaymentResult
        self.PaymentResult = PaymentResult
        owner = make_user('msgowner', [GROUP_REQUESTER])
        self.finance = make_user('msgfinance', [GROUP_FINANCE])
        self.req = TravelRequest.objects.create(
            user=owner, event=make_event(), state=RequestState.SUBMITTED_FOR_PAYMENT, description='T')
        Expense.objects.create(
            request=self.req, category='flight', description='F', estimated_amount=100,
            estimated_currency='EUR', approved_amount=100, approved_currency='EUR',
            actual_amount=90, actual_currency='EUR')

    def pay(self, result):
        from django.contrib.messages import get_messages
        from django.urls import reverse
        gateway = mock.Mock(label='Wise API Transfer')
        gateway.initiate_payment.return_value = result
        # The queue page asks again about anything still waiting.
        gateway.get_status.return_value = self.PaymentResult(status='unknown', external_id=result.external_id)
        self.client.force_login(self.finance)
        with mock.patch('payments.services.get_gateway', return_value=gateway), \
                mock.patch('payments.views.get_gateway', return_value=gateway):
            response = self.client.post(reverse('payments:process'), {
                'object_id': self.req.pk, 'gateway_id': 'wise-api'}, follow=True)
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

    def test_a_refused_payment_is_reported_as_not_paid_with_the_reason(self):
        sent = self.pay(self.PaymentResult(status='failed', metadata={'error': 'Invalid currency'}))
        self.assertEqual(sent[0][0], 'error')
        self.assertIn('was not paid', sent[0][1])
        self.assertIn('Invalid currency', sent[0][1])

    def test_a_settled_payment_is_reported_as_paid(self):
        sent = self.pay(self.PaymentResult(status='completed', external_id='1'))
        self.assertEqual(sent[0][0], 'success')

    def test_a_pending_transfer_says_it_is_waiting(self):
        sent = self.pay(self.PaymentResult(status='processing', external_id='1'))
        self.assertEqual(sent[0][0], 'info')
        self.assertIn('waiting to settle', sent[0][1])

    def test_a_transfer_wise_is_holding_says_why(self):
        held = 'Transfer processing is suspended, please ensure profile is verified.'
        sent = self.pay(self.PaymentResult(status='processing', external_id='1', metadata={'error': held}))
        self.assertEqual(sent[0][0], 'warning')
        self.assertIn(held, sent[0][1])
        self.assertIn('cannot be paid twice', sent[0][1])

    def test_the_queue_shows_a_waiting_payment_instead_of_offering_it_again(self):
        from django.urls import reverse
        self.pay(self.PaymentResult(status='processing', external_id='1'))
        with mock.patch('payments.services.get_gateway') as get_gateway:
            get_gateway.return_value.get_status.return_value = self.PaymentResult(status='unknown', external_id='1')
            html = self.client.get(reverse('payments:dashboard')).content.decode()
            get_gateway.return_value.get_status.assert_called_once_with('1')
        self.assertIn('Waiting for Wise to confirm', html)
        self.assertNotIn('Process Payment', html)

    def test_a_transfer_that_settled_meanwhile_leaves_the_queue_on_the_next_visit(self):
        from django.urls import reverse
        self.pay(self.PaymentResult(status='processing', external_id='1'))
        with mock.patch('payments.services.get_gateway') as get_gateway:
            get_gateway.return_value.get_status.return_value = self.PaymentResult(status='success', external_id='1')
            html = self.client.get(reverse('payments:dashboard')).content.decode()
        self.req.refresh_from_db()
        self.assertEqual(self.req.state, RequestState.PAID)
        self.assertNotIn(self.req.reference + ' ·', html)


class SandboxSettlingTests(TestCase):
    """A sandbox transfer only moves on when Marvin asks Wise to simulate it."""

    def client_at(self, status):
        from payments.gateways.wise_api import WiseApiClient
        wise = WiseApiClient(api_token='t', profile_id='1', sandbox=True)
        wise.get_transfer = mock.Mock(return_value={'id': 5, 'status': status})
        wise.advance_sandbox_transfer = mock.Mock(return_value={'id': 5, 'status': 'outgoing_payment_sent'})
        return wise

    def steps_run(self, wise):
        return wise.advance_sandbox_transfer.call_args.kwargs['steps'] if wise.advance_sandbox_transfer.called else ()

    def test_only_the_missing_steps_are_simulated(self):
        for status, steps in (
            # Nobody has funded it yet, so simulating it would fake a payment.
            ('incoming_payment_waiting', ()),
            ('processing', ('funds_converted', 'outgoing_payment_sent')),
            ('funds_converted', ('outgoing_payment_sent',)),
            ('outgoing_payment_sent', ()),
            ('cancelled', ()),
        ):
            with self.subTest(status=status):
                wise = self.client_at(status)
                wise.settle_sandbox_transfer(5, delay_seconds=0)
                self.assertEqual(tuple(self.steps_run(wise)), steps)

    def test_verification_still_moves_its_unfunded_test_transfer_to_processing(self):
        wise = self.client_at('incoming_payment_waiting')
        wise.fund_transfer = mock.Mock(side_effect=WiseApiError('approve in Wise', 403))
        wise.fund_or_advance_transfer(5, sandbox_delay_seconds=0, sandbox_steps=('processing',))
        self.assertEqual(tuple(self.steps_run(wise)), ('processing',))

    def test_an_unfunded_payment_is_never_simulated(self):
        wise = self.client_at('incoming_payment_waiting')
        wise.fund_transfer = mock.Mock(side_effect=WiseApiError('approve in Wise', 403))
        with self.assertRaises(WiseApiError):
            wise.fund_or_advance_transfer(5, sandbox_delay_seconds=0)
        wise.advance_sandbox_transfer.assert_not_called()

    def test_a_funded_sandbox_transfer_is_carried_through_to_sent(self):
        wise = self.client_at('processing')
        wise.fund_transfer = mock.Mock(return_value={'status': 'COMPLETED'})
        outcome = wise.fund_or_advance_transfer(5, sandbox_delay_seconds=0)
        self.assertEqual(outcome['transfer']['status'], 'outgoing_payment_sent')

    def test_a_live_funded_transfer_is_left_to_wise(self):
        wise = self.client_at('incoming_payment_waiting')
        wise.sandbox = False
        wise.fund_transfer = mock.Mock(return_value={'status': 'COMPLETED'})
        wise.fund_or_advance_transfer(5)
        wise.advance_sandbox_transfer.assert_not_called()

    def test_checking_a_stuck_sandbox_transfer_finishes_it(self):
        wise = self.client_at('processing')
        self.assertEqual(gateway_with(wise).get_status('5').status, 'success')


class QueueChoicesTests(TestCase):
    """Each row offers only what can work for it."""

    setUp = BatchExportTests.setUp

    def page(self):
        from django.urls import reverse
        self.client.force_login(self.finance)
        return self.client.get(reverse('payments:dashboard')).content.decode()

    def test_a_row_not_in_a_file_cannot_be_marked_batch_paid(self):
        html = self.page()
        self.assertNotIn('<option value="wise-csv"', html)
        self.assertNotIn('Mark Batch Paid', html)

    def test_an_exported_row_offers_only_confirming_the_batch(self):
        self.service.export_batch('wise-csv', self.finance)
        html = self.page()
        self.assertIn('Mark Batch Paid', html)
        self.assertIn('name="gateway_id" value="wise-csv"', html)


class PaymentQueuePageTests(TestCase):
    """The queue keeps a waiting payment's status current and shows what is being paid."""

    setUp = PaymentMessageTests.setUp
    pay = PaymentMessageTests.pay

    def status_json(self, result):
        from django.urls import reverse
        self.pay(self.PaymentResult(status='processing', external_id='1'))
        record = PaymentRecord.objects.get(object_id=self.req.pk)
        with mock.patch('payments.services.get_gateway') as get_gateway:
            get_gateway.return_value.get_status.return_value = result
            return self.client.get(reverse('payments:status', args=[record.pk])).json()

    def test_the_status_says_when_wise_has_paid(self):
        data = self.status_json(self.PaymentResult(status='success', external_id='1'))
        self.assertEqual((data['final'], data['paid']), (True, True))
        self.assertIn('Paid', data['message'])

    def test_the_status_says_why_wise_is_holding_a_payment(self):
        held = 'please ensure profile is verified'
        data = self.status_json(self.PaymentResult(status='processing', external_id='1', metadata={'error': held}))
        self.assertFalse(data['final'])
        self.assertIn(held, data['message'])

    def test_a_refused_transfer_is_final_and_says_it_was_not_paid(self):
        data = self.status_json(self.PaymentResult(status='failed', external_id='1',
                                                   metadata={'transfer_status': 'bounced_back'}))
        self.assertEqual((data['final'], data['paid']), (True, False))
        self.assertIn('bounced_back', data['message'])

    def test_each_row_shows_its_expenses_and_the_batch_export_comes_after_the_queue(self):
        from django.urls import reverse
        self.client.force_login(self.finance)
        html = self.client.get(reverse('payments:dashboard')).content.decode()
        self.assertIn('Expenses and documents', html)
        self.assertIn('Open the full request', html)
        self.assertIn('90.00 EUR', html)
        self.assertLess(html.index(self.req.reference + ' ·'), html.index('Export Wise Batch CSV'))


class ProfileChoiceTests(TestCase):
    """Without a configured profile, Marvin pays from the business one."""

    def test_the_business_profile_is_chosen_whatever_the_case_of_its_type(self):
        from payments.gateways.wise_api import WiseApiClient
        for business_type in ('BUSINESS', 'business'):
            with self.subTest(type=business_type):
                wise = WiseApiClient(api_token='t', sandbox=True)
                wise.get_profiles = mock.Mock(return_value=[
                    {'id': 31556187, 'type': 'PERSONAL'}, {'id': 31556188, 'type': business_type}])
                self.assertEqual(wise.resolve_profile_id(), '31556188')

    def test_a_personal_profile_is_never_paid_from(self):
        from payments.gateways.wise_api import WiseApiClient, WiseApiError
        wise = WiseApiClient(api_token='t', sandbox=True)
        wise.get_profiles = mock.Mock(return_value=[{'id': 5, 'type': 'PERSONAL'}])
        with self.assertRaises(WiseApiError):
            wise.resolve_profile_id()
