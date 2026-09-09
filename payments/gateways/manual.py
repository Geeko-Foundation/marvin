"""Built-in manual bank transfer gateway."""

from payments.base import PaymentGateway, PaymentRequest, PaymentResult


class ManualTransferGateway(PaymentGateway):
    gateway_id = 'manual'
    label = 'Manual Bank Transfer'
    description = 'Record a payment made outside the system (wire, ACH, etc.).'

    def initiate_payment(self, payment_request: PaymentRequest) -> PaymentResult:
        return PaymentResult(status='completed')
