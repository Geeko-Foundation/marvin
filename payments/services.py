"""Payment orchestration service."""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType

from payments.base import PaymentRequest
from payments.models import PaymentRecord
from payments.registry import get_adapter, get_gateway, get_default_adapter


class PaymentService:
    """Coordinates adapters and gateways to process payments."""

    def __init__(self, adapter_id: str | None = None):
        self.adapter = get_adapter(adapter_id) if adapter_id else get_default_adapter()

    def get_queue(self):
        return self.adapter.get_queue()

    def build_payment_request(self, obj) -> PaymentRequest:
        return self.adapter.build_payment_request(obj)

    def process_payment(self, object_id: int, gateway_id: str, user) -> PaymentRecord:
        obj = self.adapter.get_object(object_id)
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
            metadata=result.metadata,
            processed_by=user,
            created_by=user,
            changed_by=user,
        )

        self.adapter.on_payment_completed(obj, record, user)
        return record

    def export_batch(self, gateway_id: str) -> tuple[str | bytes, str, str]:
        gateway = get_gateway(gateway_id)
        if gateway is None:
            raise ValueError(f'Unknown gateway: {gateway_id}')
        if not gateway.supports_batch_export:
            raise ValueError(f'Gateway {gateway_id} does not support batch export.')

        queue = self.get_queue()
        requests = [self.build_payment_request(obj) for obj in queue]
        export = gateway.export_batch(requests)
        if export is None:
            raise ValueError(f'Gateway {gateway_id} returned no batch export.')
        return export.content, export.content_type, export.filename

    def refresh_status(self, record: PaymentRecord) -> PaymentRecord:
        if not record.external_id:
            return record
        gateway = get_gateway(record.gateway_id)
        if gateway is None:
            return record
        result = gateway.get_status(record.external_id)
        record.status = result.status
        record.metadata.update(result.metadata)
        record.save()
        return record
