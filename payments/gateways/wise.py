"""Wise payment gateway plugins (API transfer and batch CSV export)."""

from __future__ import annotations

import csv
import io
import uuid
from decimal import Decimal

import requests
from django.conf import settings

from marvin.validators import spreadsheet_safe
from payments.base import (
    BatchExportResult,
    PaymentGateway,
    PaymentRequest,
    PaymentResult,
)
from payments.gateways.wise_api import (
    SUPPORTED_CURRENCIES,
    WiseApiError,
    get_wise_client,
)

TRANSFER_STATUS_MAP = {
    'incoming_payment_waiting': 'processing',
    'processing': 'processing',
    'funds_converted': 'funds_converted',
    'outgoing_payment_sent': 'success',
    'cancelled': 'cancelled',
    # Sent, then sent back: the money never reached the recipient.
    'funds_refunded': 'failed',
    'bounced_back': 'failed',
    'charged_back': 'failed',
}


def settlement_details(transfer: dict, quote: dict | None = None) -> dict:
    """
    What Wise says actually happened, as opposed to what was asked for.

    Finance needs what was spent, the rate and what it was converted to. Those
    live on the transfer, with the fee on the quote behind it.
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
    quote = quote or {}
    fee = quote.get('fee')
    # A v3 quote prices each way of paying; a transfer is funded from the balance.
    for option in quote.get('paymentOptions') or ():
        if option.get('payIn') == 'BALANCE' and option.get('payOut') == 'BANK_TRANSFER':
            fee = (option.get('fee') or {}).get('total', fee)
    if fee is not None:
        details['fee'] = str(fee)
        details['fee_currency'] = quote.get('sourceCurrency') or details.get('source_currency', '')
    return details


# Currencies Wise pays in whole units only.
ZERO_DECIMAL_CURRENCIES = {'JPY', 'KRW'}


def converted(client, amount: Decimal, source: str, target: str) -> Decimal:
    """``amount`` of ``source`` in ``target`` at Wise's current rate, to the units Wise pays in."""
    places = Decimal('1') if target in ZERO_DECIMAL_CURRENCIES else Decimal('0.01')
    return (amount * client.get_rate(source, target)).quantize(places)


def transaction_id_for(payment_request: PaymentRequest, account_id: int) -> str:
    """
    The same payment always gets the same id, so a retry cannot make a second transfer.

    Wise returns the transfer it already has when it sees an id again. The id
    covers who is paid, how much and in which currency, so a changed amount or
    account makes a new transfer instead of reusing the old one. The site's
    address keeps two instances that share a Wise account apart.
    """
    amount = Decimal(str(payment_request.amount)).quantize(Decimal('0.01'))
    key = '|'.join((
        settings.SITE_BASE_URL, payment_request.reference, str(account_id),
        str(amount), payment_request.currency,
    ))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


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
        transfer = None

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

            quote = self._quote(payment_request, account_id)
            transfer = client.create_transfer(
                quote_id=quote['id'],
                target_account=account_id,
                reference=payment_request.reference,
                customer_transaction_id=transaction_id_for(payment_request, account_id),
            )
            try:
                funded = client.fund_or_advance_transfer(transfer['id'])
            except WiseApiError as exc:
                if exc.status_code != 403:
                    raise
                return self._awaiting_approval(transfer, quote, account_id)
            transfer = funded.get('transfer') or client.get_transfer(transfer['id'])

            return PaymentResult(
                status=TRANSFER_STATUS_MAP.get(transfer['status'], 'processing'),
                external_id=str(transfer['id']),
                metadata={
                    'wise_account_id': account_id,
                    'transfer_status': transfer['status'],
                    'sandbox': client.sandbox,
                    **settlement_details(transfer, quote),
                },
            )
        except (WiseApiError, requests.RequestException) as exc:
            error = {'error': str(exc), 'status_code': getattr(exc, 'status_code', 0)}
            if transfer is None:
                return PaymentResult(status='failed', metadata=error)
            # The transfer exists, so the money may already have gone. Keep its
            # id and leave it processing: the status refresh and the webhook
            # settle it, and the duplicate check refuses a second payment.
            return PaymentResult(
                status='processing',
                external_id=str(transfer['id']),
                metadata={'wise_account_id': account_id, **error},
            )

    def _awaiting_approval(self, transfer: dict, quote: dict, account_id: int, **extra) -> PaymentResult:
        """A transfer created in Wise that someone still has to fund there."""
        return PaymentResult(
            status='processing',
            external_id=str(transfer['id']),
            metadata={
                'wise_account_id': account_id,
                'transfer_status': transfer.get('status', 'incoming_payment_waiting').lower(),
                'awaiting_approval': True,
                'sandbox': self._client.sandbox,
                **settlement_details(transfer, quote),
                **extra,
            },
        )

    def send_batch(self, name: str, payment_requests: list[PaymentRequest]) -> list[PaymentResult]:
        """
        Put the payments into Wise batch groups, one per source currency, to fund in Wise.

        Returns a result for each payment, in the same order. A payment that could
        not be added fails on its own, without stopping the others.
        """
        client = self._client
        results: list[PaymentResult | None] = [None] * len(payment_requests)
        groups: dict[str, list[tuple[int, PaymentRequest, int, dict]]] = {}
        for index, payment_request in enumerate(payment_requests):
            account_id = payment_request.metadata.get('wise_account_id')
            if not account_id:
                results[index] = PaymentResult(status='failed', metadata={
                    'error': 'The requester has not set up their Wise recipient account.'})
                continue
            try:
                quote = self._quote(payment_request, int(account_id))
            except (WiseApiError, requests.RequestException) as exc:
                results[index] = PaymentResult(status='failed', metadata={'error': str(exc)})
                continue
            groups.setdefault(quote['sourceCurrency'], []).append((index, payment_request, int(account_id), quote))

        for currency, members in groups.items():
            try:
                group = client.create_batch_group(f'{name} ({currency})', currency)
            except (WiseApiError, requests.RequestException) as exc:
                for index, *_ in members:
                    results[index] = PaymentResult(status='failed', metadata={'error': str(exc)})
                continue
            for index, payment_request, account_id, quote in members:
                try:
                    transfer = client.add_batch_transfer(
                        group['id'], quote_id=quote['id'], target_account=account_id,
                        reference=payment_request.reference,
                        customer_transaction_id=transaction_id_for(payment_request, account_id),
                    )
                except (WiseApiError, requests.RequestException) as exc:
                    results[index] = PaymentResult(status='failed', metadata={'error': str(exc)})
                    continue
                results[index] = self._awaiting_approval(
                    transfer, quote, account_id, batch_group_id=group['id'], batch_name=group.get('name', name))
            try:
                client.complete_batch_group(group['id'])
            except (WiseApiError, requests.RequestException) as exc:
                # The transfers exist, so they stay recorded; the batch just has to
                # be closed in Wise before it can be funded.
                for index, *_ in members:
                    if results[index] and results[index].external_id:
                        results[index].metadata['error'] = f'The batch could not be closed: {exc}'
        return results

    def _quote(self, payment_request: PaymentRequest, account_id: int) -> dict:
        """The quote for one payment, in the currency its recipient's account takes."""
        metadata = payment_request.metadata
        client = self._client
        account_currency = metadata.get('account_currency') or payment_request.currency
        if account_currency == payment_request.currency:
            return client.create_quote(
                source_currency=metadata.get(
                    'source_currency',
                    settings.WISE_SOURCE_CURRENCY,
                ),
                target_currency=payment_request.currency,
                target_amount=payment_request.amount,
                target_account=account_id,
            )
        # The account only takes its own currency. The requester is owed the
        # approved sum in full and the foundation pays Wise's fee, so ask for
        # the approved amount's worth at Wise's rate to arrive.
        return client.create_quote(
            source_currency=payment_request.currency,
            target_currency=account_currency,
            target_amount=converted(client, payment_request.amount, payment_request.currency, account_currency),
            target_account=account_id,
        )

    def get_status(self, external_id: str) -> PaymentResult:
        """
        Poll Wise for the current transfer status by transfer ID.

        On API failure, report an unknown result so the stored status is preserved.
        """
        try:
            if self._client.sandbox:
                # Nothing else moves a sandbox transfer on, so a check does it.
                transfer = self._client.settle_sandbox_transfer(int(external_id), delay_seconds=0.5)
            else:
                transfer = self._client.get_transfer(int(external_id))
            return PaymentResult(
                status=TRANSFER_STATUS_MAP.get(transfer['status'], transfer['status']),
                external_id=external_id,
                metadata={
                    'transfer_status': transfer['status'],
                    'awaiting_approval': transfer['status'] == 'incoming_payment_waiting',
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

        Each row uses the request's own reference, as on its page and the bank
        statement. Repeat exports are prevented by the service, not by making
        references unique, since unique references would hide a duplicate.
        """
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'name', 'recipientEmail', 'paymentReference', 'receiverType',
            'amountCurrency', 'amount', 'sourceCurrency', 'targetCurrency', 'type',
        ])
        client = None
        for req in payment_requests:
            target = req.metadata.get('account_currency') or req.currency
            amount = req.amount
            if target != req.currency:
                # The amount that arrives is fixed, so Wise's fee is added to what
                # the foundation pays rather than taken off the requester's money.
                client = client or get_wise_client()
                try:
                    amount = converted(client, req.amount, req.currency, target)
                except WiseApiError as exc:
                    raise ValueError(f'Could not get Wise\'s rate for {req.reference}: {exc}') from exc
            writer.writerow(spreadsheet_safe([
                req.recipient_name,
                req.recipient_email,
                req.reference,
                'PERSON',
                target,
                amount,
                'EUR',
                target,
                'BANK_TRANSFER',
            ]))
        return BatchExportResult(
            content=output.getvalue(),
            content_type='text/csv',
            filename='Send-by-email.csv',
        )

    def supported_currencies(self) -> list[str]:
        """Return the static Wise-supported currency list used for CSV export."""
        return SUPPORTED_CURRENCIES
