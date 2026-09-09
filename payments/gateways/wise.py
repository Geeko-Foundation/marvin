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

TRANSFER_STATUS_MAP = {
    'incoming_payment_waiting': 'processing',
    'processing': 'processing',
    'funds_converted': 'funds_converted',
    'outgoing_payment_sent': 'success',
    'cancelled': 'cancelled',
}


class WiseApiGateway(PaymentGateway):
    gateway_id = 'wise-api'
    label = 'Wise API Transfer'
    description = 'Create an international transfer via the Wise REST API.'

    def __init__(self):
        self._client = get_wise_client()

    def initiate_payment(self, payment_request: PaymentRequest) -> PaymentResult:
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
                    'exchange_rate': quote.get('rate'),
                    'transfer_status': transfer['status'],
                },
            )
        except WiseApiError as exc:
            return PaymentResult(
                status='failed',
                metadata={'error': str(exc), 'status_code': exc.status_code},
            )

    def get_status(self, external_id: str) -> PaymentResult:
        try:
            transfer = self._client.get_transfer(int(external_id))
            return PaymentResult(
                status=TRANSFER_STATUS_MAP.get(transfer['status'], transfer['status']),
                external_id=external_id,
                metadata={'transfer_status': transfer['status']},
            )
        except WiseApiError:
            return PaymentResult(status='success', external_id=external_id)

    def supported_currencies(self) -> list[str]:
        try:
            return self._client.get_currencies()
        except WiseApiError:
            return SUPPORTED_CURRENCIES


class WiseCsvGateway(PaymentGateway):
    gateway_id = 'wise-csv'
    label = 'Wise Batch CSV'
    description = 'Export a Wise Send-by-email CSV for batch manual upload.'

    @property
    def supports_batch_export(self) -> bool:
        return True

    def initiate_payment(self, payment_request: PaymentRequest) -> PaymentResult:
        return PaymentResult(
            status='completed',
            metadata={'batch_method': 'wise-csv'},
        )

    def export_batch(self, payment_requests: list[PaymentRequest]) -> BatchExportResult | None:
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
        return SUPPORTED_CURRENCIES
