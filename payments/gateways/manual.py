"""Built-in manual bank transfer gateway."""

from payments.base import PaymentGateway, PaymentRequest, PaymentResult


class ManualTransferGateway(PaymentGateway):
    """
    Gateway for payments made outside Marvin (wire, ACH, etc.).

    Records the payment as immediately ``completed`` with no external API call.
    Finance uses this after executing a transfer through their bank.
    """

    gateway_id = 'manual'
    label = 'Manual Bank Transfer'
    description = 'Record a payment made outside the system (wire, ACH, etc.).'

    def initiate_payment(self, payment_request: PaymentRequest) -> PaymentResult:
        """Mark the payment completed; no gateway integration is invoked."""
        return PaymentResult(status='completed')
