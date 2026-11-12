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
