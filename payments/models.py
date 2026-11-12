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
