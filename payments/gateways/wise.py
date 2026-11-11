"""Wise payment gateway plugins (API transfer and batch CSV export)."""

from __future__ import annotations

import csv
import io
import time
from decimal import Decimal

from django.conf import settings

from payments.base import (
    BatchExportResult,
    PaymentGateway,
    PaymentRequest,
    PaymentResult,
)
from payments.gateways.wise_api import SUPPORTED_CURRENCIES, WiseApiError, get_wise_client

# Map Wise transfer lifecycle states to internal PaymentRecord status values.
TRANSFER_STATUS_MAP = {
    'incoming_payment_waiting': 'processing',
    'processing': 'processing',
    'funds_converted': 'funds_converted',
    'outgoing_payment_sent': 'success',
    'cancelled': 'cancelled',
}


def settlement_details(transfer: dict, quote: dict | None = None) -> dict:
    """
    What Wise says actually happened, as opposed to what was asked for.

    Patrick wanted "exactly how much has been spent and what the conversion
    rate was and what the target currency was converted to". Those live on the
    transfer, with the fee on the quote behind it.
    """
    details = {}
    for key, field in (
        ('source_amount', 'sourceValue'),
        ('source_currency', 'sourceCurrency'),
        ('target_amount', 'targetValue'),
        ('target_currency', 'targetCurrency'),
    ):
        value = transfer.get(field)
        if value not in (None, ''):
            details[key] = str(value)

    # The transfer's own rate is authoritative once it has settled; the
    # quote's is what was offered beforehand.
    rate = transfer.get('rate') or (quote or {}).get('rate')
    if rate:
        details['exchange_rate'] = str(rate)
    fee = (quote or {}).get('fee')
    if fee is not None:
        details['fee'] = str(fee)
    return details


class WiseApiGateway(PaymentGateway):
    """
    Live Wise REST API gateway.

    Creates a quote, transfer, and funds (or sandbox-simulates) each payment
    using the recipient's stored ``wise_account_id`` from profile metadata.
    """

    gateway_id = 'wise-api'
    label = 'Wise API Transfer'
    description = 'Create an international transfer via the Wise REST API.'

    def __init__(self):
        """Initialize with a client built from Django Wise settings."""
        self._client = get_wise_client()

    def initiate_payment(self, payment_request: PaymentRequest) -> PaymentResult:
        """
        Execute quote → transfer → fund for one reimbursement.

        Requires ``payment_request.metadata['wise_account_id']``. Returns
        ``failed`` when the recipient has not completed Wise onboarding.
        """
        metadata = payment_request.metadata
        client = self._client

        try:
            account_id = metadata.get('wise_account_id')
            if not account_id:
                return PaymentResult(
                    status='failed',
                    metadata={
                        'error': (
                            'Requestor has not set up their Wise recipient account. '
                            'They must complete onboarding before payment can be processed.'
                        ),
                    },
                )
            account_id = int(account_id)

            quote = client.create_quote(
                source_currency=metadata.get(
                    'source_currency',
                    getattr(settings, 'WISE_SOURCE_CURRENCY', 'GBP'),
                ),
                target_currency=payment_request.currency,
                target_amount=payment_request.amount,
                target_account=account_id,
            )
            transfer = client.create_transfer(
                quote_id=quote['id'],
                target_account=account_id,
                reference=payment_request.reference,
            )
            client.fund_or_advance_transfer(transfer['id'])
            transfer = client.get_transfer(transfer['id'])

            return PaymentResult(
                status=TRANSFER_STATUS_MAP.get(transfer['status'], 'processing'),
                external_id=str(transfer['id']),
                metadata={
                    'wise_account_id': account_id,
                    'transfer_status': transfer['status'],
                    **settlement_details(transfer, quote),
                },
            )
        except WiseApiError as exc:
            return PaymentResult(
                status='failed',
                metadata={'error': str(exc), 'status_code': exc.status_code},
            )

    def get_status(self, external_id: str) -> PaymentResult:
        """
        Poll Wise for the current transfer status by transfer ID.

        On API failure, report an unknown result so the stored status is preserved.
        """
        try:
            transfer = self._client.get_transfer(int(external_id))
            return PaymentResult(
                status=TRANSFER_STATUS_MAP.get(transfer['status'], transfer['status']),
                external_id=external_id,
                metadata={
                    'transfer_status': transfer['status'],
                    **settlement_details(transfer),
                },
            )
        except WiseApiError:
            return PaymentResult(status='unknown', external_id=external_id)

    def supported_currencies(self) -> list[str]:
        """Return currencies from the Wise API, falling back to a static list."""
        try:
            return self._client.get_currencies()
        except WiseApiError:
            return SUPPORTED_CURRENCIES


class WiseCsvGateway(PaymentGateway):
    """
    Batch CSV export gateway for Wise Send-by-email uploads.

    Does not initiate individual API transfers; marks each item ``completed``
    and produces a downloadable CSV for manual batch processing in Wise.
    """

    gateway_id = 'wise-csv'
    label = 'Wise Batch CSV'
    description = 'Export a Wise Send-by-email CSV for batch manual upload.'

    @property
    def supports_batch_export(self) -> bool:
        """This gateway only supports batch file export, not live API transfers."""
        return True

    def initiate_payment(self, payment_request: PaymentRequest) -> PaymentResult:
        """
        Record a placeholder result for queue items included in a batch export.

        Actual payout happens when finance uploads the CSV to Wise manually.
        """
        return PaymentResult(
            status='completed',
            metadata={'batch_method': 'wise-csv'},
        )

    def export_batch(self, payment_requests: list[PaymentRequest]) -> BatchExportResult | None:
        """
        Build a Wise Send-by-email CSV from the current payment queue.

        Each row includes recipient details and a unique payment reference
        suffix to avoid collisions on repeated exports.
        """
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'name', 'recipientEmail', 'paymentReference', 'receiverType',
            'amountCurrency', 'amount', 'sourceCurrency', 'targetCurrency', 'type',
        ])
        for req in payment_requests:
            reference = f'{req.reference}-{int(time.time())}'
            writer.writerow([
                req.recipient_name,
                req.recipient_email,
                reference,
                'PERSON',
                req.currency,
                req.amount,
                'EUR',
                req.currency,
                'BANK_TRANSFER',
            ])
        return BatchExportResult(
            content=output.getvalue(),
            content_type='text/csv',
            filename='Send-by-email.csv',
        )

    def supported_currencies(self) -> list[str]:
        """Return the static Wise-supported currency list used for CSV export."""
        return SUPPORTED_CURRENCIES
