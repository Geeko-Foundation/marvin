from unittest import mock

from django.test import TestCase

from payments.base import PaymentResult
from payments.gateways.wise import WiseApiGateway
from payments.gateways.wise_api import WiseApiError
from payments.services import PaymentService
from profile.groups import GROUP_REQUESTER
from tsp.models import Expense, RequestState, StateHistory, TravelRequest
from tsp.tests import make_event, make_user


class PaymentStatusTests(TestCase):
    def setUp(self):
        self.owner = make_user('paymentowner', [GROUP_REQUESTER])
        self.request = TravelRequest.objects.create(
            user=self.owner, event=make_event(), state=RequestState.SUBMITTED_FOR_PAYMENT,
        )
        Expense.objects.create(request=self.request, category='flight',
            estimated_amount=100, approved_amount=100, approved_currency='EUR',
            actual_amount=75, actual_currency='EUR')
        self.service = PaymentService()
        self.gateway = mock.Mock()

    def process(self, status):
        self.gateway.initiate_payment.return_value = PaymentResult(status=status, external_id='123')
        with mock.patch('payments.services.get_gateway', return_value=self.gateway):
            return self.service.process_payment(self.request.pk, 'wise-api', self.owner)

    def test_pending_transfer_does_not_mark_request_paid(self):
        self.process('processing')
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_failed_transfer_does_not_mark_request_paid(self):
        self.process('failed')
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_successful_transfer_marks_request_paid(self):
        self.process('success')
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.PAID)

    def test_pending_transfer_cannot_be_started_again(self):
        self.process('processing')
        with self.assertRaisesMessage(ValueError, 'already exists'):
            self.process('processing')
        self.assertEqual(self.gateway.initiate_payment.call_count, 1)

    def test_polling_completes_a_pending_request_once(self):
        record = self.process('processing')
        self.gateway.get_status.return_value = PaymentResult(status='success', external_id='123')
        with mock.patch('payments.services.get_gateway', return_value=self.gateway):
            self.service.refresh_status(record)
            self.service.refresh_status(record)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestState.PAID)
        self.assertEqual(StateHistory.objects.filter(request=self.request, to_state=RequestState.PAID).count(), 1)

    def test_unknown_poll_result_preserves_the_pending_status(self):
        record = self.process('processing')
        self.gateway.get_status.return_value = PaymentResult(status='unknown', external_id='123')
        with mock.patch('payments.services.get_gateway', return_value=self.gateway):
            self.service.refresh_status(record)
        record.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(record.status, 'processing')
        self.assertEqual(self.request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_wise_error_does_not_report_success(self):
        gateway = WiseApiGateway()
        with mock.patch.object(gateway._client, 'get_transfer', side_effect=WiseApiError('offline')):
            self.assertEqual(gateway.get_status('123').status, 'unknown')
