"""Payment gateway plugin interface and shared data types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class PaymentRequest:
    """Normalized payment instruction passed to a gateway."""

    amount: Decimal
    currency: str
    recipient_name: str
    recipient_email: str
    reference: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentResult:
    """Result returned by a gateway after initiating or polling a payment."""

    status: str = 'completed'
    external_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchExportResult:
    """File export produced by a gateway batch operation."""

    content: str | bytes
    content_type: str
    filename: str


class PaymentGateway(ABC):
    """
    Base class for payment gateway plugins.

    Subclass this in your own project and register the dotted path in
    settings.PAYMENTS_GATEWAYS.
    """

    gateway_id: str
    label: str
    description: str = ''

    @abstractmethod
    def initiate_payment(self, payment_request: PaymentRequest) -> PaymentResult:
        """Initiate a single payment through this gateway."""

    def get_status(self, external_id: str) -> PaymentResult:
        """Poll payment status. Override for async gateways."""
        return PaymentResult(status='completed', external_id=external_id)

    def export_batch(self, payment_requests: list[PaymentRequest]) -> BatchExportResult | None:
        """Export a batch file for manual upload. Override when supported."""
        return None

    def supported_currencies(self) -> list[str]:
        """Return ISO currency codes supported by this gateway."""
        return []

    @property
    def supports_batch_export(self) -> bool:
        """True when ``export_batch`` is implemented for file-based payouts."""
        return False


class PayableAdapter(ABC):
    """
    Connects a host-project model to the payments workflow.

    Subclass this for each payable object type (e.g. travel requests,
    invoices) and register in settings.PAYMENTS_ADAPTERS.
    """

    adapter_id: str
    label: str

    @abstractmethod
    def get_queue(self):
        """Return queryset of objects awaiting payment."""

    @abstractmethod
    def get_object(self, object_id: int):
        """Fetch a single payable object by primary key."""

    @abstractmethod
    def build_payment_request(self, obj) -> PaymentRequest:
        """Build a normalized PaymentRequest from a payable object."""

    @abstractmethod
    def on_payment_completed(self, obj, payment_record, user) -> None:
        """Host-project hook called after a payment is recorded."""

    def get_detail_url(self, obj) -> str:
        """URL name or path for linking to the payable object detail page."""
        return ''

    def get_queue_item_context(self, obj) -> dict[str, Any]:
        """Extra template context for a queue item."""
        return {}

    def payment_reference(self, obj) -> str:
        """Generate a payment reference string for this object."""
        return f'{self.adapter_id}-{obj.pk}'
