"""Payment orchestration service."""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from payments.base import PaymentRequest
from payments.models import PaymentRecord
from profile.audit_log import record as audit
from payments.registry import get_adapter, get_gateway, get_default_adapter


class PaymentService:
    """
    High-level coordinator between payable adapters and payment gateways.

    A single adapter (e.g. travel requests) supplies queue items and builds
    ``PaymentRequest`` payloads; gateways execute transfers or batch exports.
    """

    def __init__(self, adapter_id: str | None = None):
        """
        Bind this service to one payable adapter.

        Args:
            adapter_id: Explicit adapter key from ``settings.PAYMENTS_ADAPTERS``.
                When omitted, ``get_default_adapter()`` is used.
        """
        self.adapter = get_adapter(adapter_id) if adapter_id else get_default_adapter()

    def get_queue(self):
        """Return the queryset of payable objects awaiting payment for this adapter."""
        return self.adapter.get_queue()

    def build_payment_request(self, obj) -> PaymentRequest:
        """Build a normalized payment instruction from a single payable object."""
        return self.adapter.build_payment_request(obj)

    @transaction.atomic
    def process_payment(self, object_id: int, gateway_id: str, user) -> PaymentRecord:
        """
        Initiate payment for one queue item and persist the result.

        Steps:
        1. Load the payable object via the adapter.
        2. Call ``gateway.initiate_payment`` with the built request.
        3. Create a ``PaymentRecord`` linked to the object via GenericForeignKey.
        4. Invoke ``adapter.on_payment_completed`` (e.g. mark travel request paid).

        Raises:
            ValueError: Unknown ``gateway_id``.
        """
        obj = self.adapter.get_object(object_id)
        obj = type(obj)._default_manager.select_for_update().get(pk=obj.pk)
        if not self.adapter.get_queue().filter(pk=obj.pk).exists():
            raise ValueError('This request is no longer awaiting payment.')
        ct = ContentType.objects.get_for_model(obj)
        purpose = self.adapter.payment_purpose(obj)
        existing = self._payments_under_way(obj)
        if gateway_id == 'wise-csv':
            # Finance confirming that the batch file it was in went through.
            if not existing.filter(status='exported').exists():
                raise ValueError(
                    'This request has not been exported in a batch file, so no money has '
                    'been sent. Export the batch and upload it to Wise first.'
                )
            existing = existing.exclude(status='exported')
        if existing.filter(status='exported').exists():
            raise ValueError(
                'This request is in an exported batch file. Mark it paid with Wise Batch CSV '
                'once Wise has processed the file, rather than paying it again.'
            )
        if existing.exists():
            raise ValueError('A payment already exists for this request. Check its status before retrying.')
        gateway = get_gateway(gateway_id)
        if gateway is None:
            raise ValueError(f'Unknown gateway: {gateway_id}')

        payment_request = self.adapter.build_payment_request(obj)
        result = gateway.initiate_payment(payment_request)

        ct = ContentType.objects.get_for_model(obj)
        record = PaymentRecord.objects.create(
            content_type=ct,
            object_id=obj.pk,
            gateway_id=gateway_id,
            amount=payment_request.amount,
            currency=payment_request.currency,
            recipient_name=payment_request.recipient_name,
            recipient_email=payment_request.recipient_email,
            reference=payment_request.reference,
            external_id=result.external_id,
            status=result.status,
            purpose=purpose,
            metadata=result.metadata,
            processed_by=user,
            created_by=user,
            changed_by=user,
        )

        if record.status in {'success', 'completed'}:
            self.adapter.on_payment_completed(obj, record, user)
        audit(
            'payment.processed',
            user=user,
            target=f'{gateway_id} {record.reference}',
            detail=f'{record.amount} {record.currency}, status {record.status}',
        )
        return record

    def _payments_under_way(self, obj):
        """Records for ``obj`` that mean money is moving, or about to, for its current purpose."""
        return PaymentRecord.objects.filter(
            content_type=ContentType.objects.get_for_model(obj),
            object_id=obj.pk,
            purpose=self.adapter.payment_purpose(obj),
        ).exclude(status__in=['failed', 'cancelled'])

    def export_batch(self, gateway_id: str, user=None) -> tuple[str | bytes, str, str]:
        """
        Export all current queue items as a batch file for manual gateway upload.

        Returns:
            Tuple of ``(file_content, content_type, filename)``.

        Raises:
            ValueError: Unknown gateway, unsupported batch export, or empty export.
        """
        gateway = get_gateway(gateway_id)
        if gateway is None:
            raise ValueError(f'Unknown gateway: {gateway_id}')
        if not gateway.supports_batch_export:
            raise ValueError(f'Gateway {gateway_id} does not support batch export.')

        # Anything already in a file, or being paid another way, stays out, so
        # exporting again cannot put the same person in two files.
        # shortcut: two exports at the same instant could both include a request;
        # fine for one finance team, lock the queue rows if that ever changes.
        pending = [obj for obj in self.get_queue() if not self._payments_under_way(obj).exists()]
        if not pending:
            raise ValueError('Everything awaiting payment is already in an exported file or being paid.')
        requests = [self.build_payment_request(obj) for obj in pending]
        export = gateway.export_batch(requests)
        if export is None:
            raise ValueError(f'Gateway {gateway_id} returned no batch export.')
        with transaction.atomic():
            for obj, payment_request in zip(pending, requests):
                PaymentRecord.objects.create(
                    content_type=ContentType.objects.get_for_model(obj),
                    object_id=obj.pk,
                    gateway_id=gateway_id,
                    amount=payment_request.amount,
                    currency=payment_request.currency,
                    recipient_name=payment_request.recipient_name,
                    recipient_email=payment_request.recipient_email,
                    reference=payment_request.reference,
                    status='exported',
                    purpose=self.adapter.payment_purpose(obj),
                    metadata={'batch_file': export.filename},
                    processed_by=user,
                    created_by=user,
                    changed_by=user,
                )
        return export.content, export.content_type, export.filename

    def refresh_status(self, record: PaymentRecord) -> PaymentRecord:
        """
        Poll the gateway for the latest status of an existing payment record.

        No-op when ``external_id`` is missing or the gateway is unknown.
        Updates ``status`` and merges ``metadata`` on the record before saving.
        """
        if not record.external_id:
            return record
        gateway = get_gateway(record.gateway_id)
        if gateway is None:
            return record
        result = gateway.get_status(record.external_id)
        if result.status != 'unknown':
            record.status = result.status
        record.metadata.update(result.metadata)
        record.save()
        if record.status in {'success', 'completed'}:
            obj = record.content_object
            if obj is not None:
                self.adapter.on_payment_completed(obj, record, record.processed_by)
        return record
