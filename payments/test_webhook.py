import base64
import json
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import TestCase, override_settings
from django.urls import reverse

from payments.base import PaymentResult
from payments.models import PaymentRecord
from profile.groups import GROUP_REQUESTER
from tsp.models import Expense, RequestState, TravelRequest
from tsp.tests import make_event, make_user

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = _KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def sign(body: bytes, key=_KEY) -> str:
    """Sign a body the way Wise does, over the exact bytes sent."""
    return base64.b64encode(
        key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    ).decode()


@override_settings(WISE_WEBHOOK_PUBLIC_KEY=PUBLIC_PEM)
class WiseWebhookTests(TestCase):
    """Wise tells us a transfer moved; we check who is asking, then look."""

    def setUp(self):
        self.owner = make_user('webhookowner', [GROUP_REQUESTER])
        self.request = TravelRequest.objects.create(
            user=self.owner, event=make_event(),
            state=RequestState.SUBMITTED_FOR_PAYMENT,
        )
        Expense.objects.create(
            request=self.request, category='transportation',
            estimated_amount=100, approved_amount=100, approved_currency='EUR',
            actual_amount=75, actual_currency='EUR')
        self.record = PaymentRecord.objects.create(
            content_object=self.request, gateway_id='wise-api',
            external_id='2149768611', amount=75, currency='EUR',
            status='processing', processed_by=self.owner,
        )
        self.url = reverse('payments:wise_webhook')

    def event(self, transfer_id='2149768611', state='outgoing_payment_sent'):
        return json.dumps({
            'event_type': 'transfers#state-change',
            'data': {'resource': {'id': int(transfer_id), 'type': 'transfer'},
                     'current_state': state},
        }).encode()

    def post(self, body, signature=None, **headers):
        return self.client.post(
            self.url, data=body, content_type='application/json',
            headers={'x-signature-sha256': signature if signature is not None else sign(body),
                     **headers},
        )

    def gateway_saying(self, status):
        gateway = mock.Mock()
        gateway.get_status.return_value = PaymentResult(
            status=status, external_id='2149768611', metadata={})
        return mock.patch('payments.services.get_gateway', return_value=gateway)

    def test_a_signed_event_marks_the_request_paid_once_wise_confirms(self):
        with self.gateway_saying('success'):
            response = self.post(self.event())
        self.assertEqual(response.status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.PAID)

    def test_the_event_cannot_claim_the_money_moved_by_itself(self):
        # Wise says the transfer is still processing, whatever the event said.
        with self.gateway_saying('processing'):
            self.post(self.event(state='outgoing_payment_sent'))
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_a_forged_signature_changes_nothing(self):
        body = self.event()
        with self.gateway_saying('success'):
            response = self.post(body, signature=sign(body, OTHER_KEY))
        self.assertEqual(response.status_code, 400)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_an_unsigned_delivery_changes_nothing(self):
        with self.gateway_saying('success'):
            response = self.post(self.event(), signature='')
        self.assertEqual(response.status_code, 400)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_a_signature_for_a_different_body_changes_nothing(self):
        # A replayed signature from an earlier delivery must not carry a new body.
        with self.gateway_saying('success'):
            response = self.post(self.event(), signature=sign(self.event(transfer_id='999')))
        self.assertEqual(response.status_code, 400)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    @override_settings(WISE_WEBHOOK_PUBLIC_KEY='')
    def test_without_a_signing_key_the_endpoint_refuses_to_act(self):
        with self.gateway_saying('success'):
            response = self.post(self.event(), signature='anything')
        self.assertEqual(response.status_code, 503)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_a_repeated_delivery_leaves_the_request_paid_and_does_not_error(self):
        with self.gateway_saying('success'):
            self.post(self.event())
            response = self.post(self.event())
        self.assertEqual(response.status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.PAID)
        self.assertEqual(
            self.request.state_history.filter(to_state=RequestState.PAID).count(), 1)

    def test_a_transfer_we_have_no_record_of_is_accepted_and_ignored(self):
        # Answering non-2xx would make Wise retry a transfer that is not ours.
        response = self.post(self.event(transfer_id='404404'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('no record', response.json()['status'])

    def test_other_event_types_are_accepted_and_ignored(self):
        body = json.dumps({'event_type': 'balances#credit', 'data': {}}).encode()
        response = self.post(body)
        self.assertEqual(response.status_code, 200)
        self.assertIn('ignored', response.json()['status'])

    def test_a_test_notification_is_acknowledged_without_touching_anything(self):
        response = self.post(self.event(), **{'x-test-notification': 'true'})
        self.assertEqual(response.status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_a_signed_but_unreadable_body_is_refused(self):
        response = self.post(b'not json at all')
        self.assertEqual(response.status_code, 400)

    def test_the_endpoint_does_not_answer_a_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
