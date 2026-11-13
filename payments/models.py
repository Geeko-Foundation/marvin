import re

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from marvin.audit import AuditModel


class PaymentRecord(AuditModel):
    """Gateway-agnostic record of a processed payment."""

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    gateway_id = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    recipient_name = models.CharField(max_length=200)
    recipient_email = models.EmailField()
    reference = models.CharField(max_length=100)
    external_id = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=30, default='completed')
    # What the money is for, as the payable adapter names it: ``payment`` for
    # the settlement, or something like ``advance`` paid ahead of it.
    purpose = models.CharField(max_length=20, default='payment')
    metadata = models.JSONField(default=dict, blank=True)
    processed_by = models.ForeignKey(
        get_user_model(),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='processed_payments',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
            models.Index(fields=['gateway_id']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        """Short label for admin lists and debugging."""
        return f'Payment {self.reference} via {self.gateway_id}'

    @property
    def method_label(self):
        """Human-readable gateway name for templates (falls back to ``gateway_id``)."""
        from payments.registry import get_gateway

        gateway = get_gateway(self.gateway_id)
        return gateway.label if gateway else self.gateway_id

    def get_status_display(self):
        """Format ``status`` for display (underscores to spaces, title case)."""
        return self.status.replace('_', ' ').title()

    def save(self, *args, **kwargs):
        """Default ``created_by`` from ``processed_by`` when not already set."""
        if self.processed_by_id and not self.created_by_id:
            self.created_by = self.processed_by
        super().save(*args, **kwargs)


# Keys whose values are bank details. They are kept to the last four
# characters, so the log can show which account without storing it.
MASKED_KEYS = {
    'iban', 'accountnumber', 'sortcode', 'routingnumber', 'ifsccode', 'bsbcode',
    'bankcode', 'branchcode', 'swiftcode', 'bic', 'cpf', 'taxid',
}


def masked(value):
    """``value`` with bank details cut down to their last four characters."""
    if isinstance(value, dict):
        return {
            key: (f'…{str(item)[-4:]}' if key.lower() in MASKED_KEYS and item else masked(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


class WiseLogEntry(models.Model):
    """
    One exchange with Wise, written once and never changed.

    Every API call Marvin makes and every webhook Wise sends, with what was
    sent and what came back, so a payment's whole story can be read later.
    The API token and signature headers are never stored.
    """

    API = 'api'
    WEBHOOK = 'webhook'

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=10, choices=[(API, 'API call'), (WEBHOOK, 'Webhook')])
    mode = models.CharField(max_length=10, blank=True)
    method = models.CharField(max_length=10, blank=True)
    path = models.CharField(max_length=300)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    request_body = models.JSONField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    transfer_id = models.CharField(max_length=40, blank=True, db_index=True)
    signature_valid = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Wise log entries'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M:%S} {self.method} {self.path} {self.status_code}'

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError('Wise log entries cannot be changed once written.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('Wise log entries cannot be deleted.')


def transfer_id_in(path, body):
    """The transfer an exchange is about, from its path or its body."""
    found = re.search(r'/transfers/(\d+)', path)
    if found:
        return found.group(1)
    if isinstance(body, dict):
        resource = (body.get('data') or {}).get('resource') or {}
        for candidate in (resource.get('id'), body.get('id') if body.get('targetAccount') else None):
            if candidate:
                return str(candidate)
    return ''
