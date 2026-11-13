from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from payments.gateways.wise import WiseApiGateway
from payments.gateways.wise_api import WiseApiClient, WiseApiError
from payments.models import PaymentRecord, WiseLogEntry, masked
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER
from tsp.models import Expense, RequestState, TravelRequest
from tsp.tests import make_event, make_user


def fake_wise():
    wise = mock.Mock(sandbox=True)
    wise.create_quote.side_effect = lambda **kw: {'id': f'q-{kw["target_amount"]}', 'sourceCurrency': 'EUR'}
    wise.create_batch_group.return_value = {'id': 'group-1', 'name': 'Marvin run (EUR)'}
    counter = iter(range(5000, 5100))
    wise.add_batch_transfer.side_effect = lambda *a, **kw: {'id': next(counter), 'status': 'INCOMING_PAYMENT_WAITING'}
    return wise


class BatchSendingTests(TestCase):
    """Everything waiting goes to Wise as one batch, to be funded there."""

    def setUp(self):
        self.finance = make_user('batchfinance', [GROUP_FINANCE])
        self.requests = []
        for name in ('batchone', 'batchtwo'):
            req = TravelRequest.objects.create(user=make_user(name, [GROUP_REQUESTER]), event=make_event(),
                                               state=RequestState.SUBMITTED_FOR_PAYMENT, description='T')
            Expense.objects.create(request=req, category='flight', description='F', estimated_amount=100,
                                   estimated_currency='EUR', approved_amount=100, approved_currency='EUR',
                                   actual_amount=90, actual_currency='EUR')
            self.requests.append(req)

    def send(self, wise):
        """Press the button with Wise faked, and return the messages shown."""
        from django.contrib.messages import get_messages
        self.client.force_login(self.finance)
        with mock.patch('payments.gateways.wise.get_wise_client', return_value=wise):
            gateway = WiseApiGateway()
        with mock.patch('payments.services.get_gateway', return_value=gateway):
            response = self.client.post(reverse('payments:send_wise_batch'))
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def test_each_payment_becomes_a_transfer_in_one_closed_batch(self):
        wise = fake_wise()
        self.assertIn('Sent 2 payments to Wise as a batch', self.send(wise))
        wise.create_batch_group.assert_called_once()
        self.assertEqual(wise.add_batch_transfer.call_count, 2)
        wise.complete_batch_group.assert_called_once_with('group-1')
        records = PaymentRecord.objects.filter(gateway_id='wise-api')
        self.assertEqual(sorted(r.reference for r in records), sorted(r.reference for r in self.requests))
        for record in records:
            self.assertEqual(record.status, 'processing')
            self.assertTrue(record.metadata['awaiting_approval'])
            self.assertEqual(record.metadata['batch_group_id'], 'group-1')

    def test_a_second_press_adds_nothing_twice(self):
        self.send(fake_wise())
        wise = fake_wise()
        self.assertIn('Nothing is waiting to be sent to Wise', self.send(wise))
        wise.create_batch_group.assert_not_called()
        self.assertEqual(PaymentRecord.objects.count(), 2)

    def test_a_payment_wise_refuses_is_reported_and_the_rest_still_go(self):
        wise = fake_wise()
        first = wise.add_batch_transfer.side_effect
        calls = iter([WiseApiError('recipient closed'), None])

        def add(*args, **kwargs):
            problem = next(calls)
            if problem:
                raise problem
            return first(*args, **kwargs)
        wise.add_batch_transfer.side_effect = add
        self.assertIn('recipient closed', self.send(wise))
        self.assertEqual(PaymentRecord.objects.count(), 1)

    def test_the_queue_says_to_approve_it_in_wise(self):
        self.send(fake_wise())
        with mock.patch('payments.services.get_gateway') as get_gateway:
            from payments.base import PaymentResult
            get_gateway.return_value.get_status.return_value = PaymentResult(status='unknown')
            html = self.client.get(reverse('payments:dashboard')).content.decode()
        self.assertIn('Approve it in Wise', html)
        self.assertIn('Marvin run (EUR)', html)
        self.assertNotIn('Process Payment', html)


class ApprovalInWiseTests(TestCase):
    """When Wise wants the payment approved in Wise, that is the next step, not an error."""

    def test_a_refused_funding_is_recorded_as_waiting_for_approval(self):
        wise = fake_wise()
        wise.create_transfer.return_value = {'id': 77, 'status': 'incoming_payment_waiting'}
        wise.fund_or_advance_transfer.side_effect = WiseApiError('Created in Wise. Approve it there.', 403)
        from payments.base import PaymentRequest
        with mock.patch('payments.gateways.wise.get_wise_client', return_value=wise):
            result = WiseApiGateway().initiate_payment(PaymentRequest(
                amount=Decimal('10'), currency='EUR', recipient_name='x', recipient_email='x@example.org',
                reference='TSP1', metadata={'wise_account_id': 9}))
        self.assertEqual((result.status, result.external_id), ('processing', '77'))
        self.assertTrue(result.metadata['awaiting_approval'])
        self.assertNotIn('error', result.metadata)


class WiseLogTests(TestCase):
    """Every exchange with Wise is kept, with bank details cut short."""

    def test_bank_details_keep_only_their_last_four_characters(self):
        body = {'details': {'IBAN': 'DE89370400440532013000', 'legalType': 'PRIVATE'},
                'accounts': [{'accountNumber': '12345678'}]}
        self.assertEqual(masked(body), {'details': {'IBAN': '…3000', 'legalType': 'PRIVATE'},
                                        'accounts': [{'accountNumber': '…5678'}]})

    def test_each_api_call_is_recorded_without_the_token(self):
        wise = WiseApiClient(api_token='secret-token', profile_id='1', sandbox=True)
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = {'id': 42, 'status': 'processing'}
        with mock.patch.object(wise._session, 'request', return_value=response):
            wise.get_transfer(42)
        entry = WiseLogEntry.objects.get()
        self.assertEqual((entry.method, entry.path, entry.status_code, entry.transfer_id),
                         ('GET', '/v1/transfers/42', 200, '42'))
        self.assertNotIn('secret-token', str(entry.request_body) + str(entry.response_body))

    def test_an_unreachable_wise_is_recorded_too(self):
        import requests
        wise = WiseApiClient(api_token='t', profile_id='1', sandbox=True)
        with mock.patch.object(wise._session, 'request', side_effect=requests.Timeout('slow')):
            with self.assertRaises(WiseApiError):
                wise.get_transfer(42)
        self.assertIn('slow', WiseLogEntry.objects.get().response_body['error'])

    def test_webhooks_are_recorded_signed_or_not(self):
        self.client.post(reverse('payments:wise_webhook'), data=b'{"data": {"resource": {"id": 9}}}',
                         content_type='application/json')
        entry = WiseLogEntry.objects.get(kind=WiseLogEntry.WEBHOOK)
        self.assertEqual((entry.transfer_id, entry.signature_valid), ('9', False))

    def test_entries_cannot_be_changed_or_deleted(self):
        entry = WiseLogEntry.objects.create(kind=WiseLogEntry.API, path='/x')
        with self.assertRaises(ValueError):
            entry.save()
        with self.assertRaises(ValueError):
            entry.delete()
