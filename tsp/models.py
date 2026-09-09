from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.utils import timezone

from marvin.audit import AuditModel


class RequestState(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    SUBMITTED = 'submitted', 'Submitted'
    APPROVED = 'approved', 'Approved'
    REJECTED = 'rejected', 'Rejected'
    ACCEPTED = 'accepted', 'Accepted'
    SUBMITTED_FOR_PAYMENT = 'submitted_for_payment', 'Submitted for Payment'
    PAID = 'paid', 'Paid'
    CANCELLED = 'cancelled', 'Cancelled'


class ExpenseCategory(models.TextChoices):
    TRANSPORTATION = 'transportation', 'Transportation'
    LODGING = 'lodging', 'Lodging'
    MEALS = 'meals', 'Meals'
    REGISTRATION = 'registration', 'Registration'
    VISA = 'visa', 'Visa Fees'
    OTHER = 'other', 'Other'


class DocumentKind(models.TextChoices):
    RECEIPT = 'receipt', 'Receipt'
    INVOICE = 'invoice', 'Invoice'
    SIGNED_FORM = 'signed_form', 'Signed Form'


class Event(AuditModel):
    name = models.CharField(max_length=200)
    location = models.CharField(max_length=200)
    start_date = models.DateField()
    end_date = models.DateField()
    accepting_requests = models.BooleanField(default=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return self.name

    @property
    def is_past(self):
        return self.end_date < timezone.now().date()


class TravelRequest(AuditModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='travel_requests',
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='requests')
    talk_url = models.URLField(blank=True, null=True)
    talk_title = models.CharField(max_length=300, blank=True, null=True)
    description = models.TextField()
    state = models.CharField(
        max_length=30,
        choices=RequestState.choices,
        default=RequestState.DRAFT,
    )
    approved_at = models.DateTimeField(blank=True, null=True)
    accepted_at = models.DateTimeField(blank=True, null=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='accepted_requests',
    )
    payments = GenericRelation(
        'payments.PaymentRecord',
        content_type_field='content_type',
        object_id_field='object_id',
    )

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'Request #{self.pk} — {self.event.name}'

    @property
    def is_editable_by_requester(self):
        return self.state in (RequestState.DRAFT, RequestState.SUBMITTED)

    def total_estimated(self):
        return sum(e.estimated_amount for e in self.expenses.all())

    def total_approved(self):
        return sum(e.approved_amount or 0 for e in self.expenses.all())

    def has_all_required_documents(self):
        kinds = set(self.documents.values_list('kind', flat=True))
        required = {DocumentKind.RECEIPT, DocumentKind.INVOICE, DocumentKind.SIGNED_FORM}
        return required.issubset(kinds)

    def missing_document_kinds(self):
        kinds = set(self.documents.values_list('kind', flat=True))
        required = [
            (DocumentKind.RECEIPT, 'Receipt'),
            (DocumentKind.INVOICE, 'Invoice'),
            (DocumentKind.SIGNED_FORM, 'Signed Form'),
        ]
        return [label for kind, label in required if kind not in kinds]

    @property
    def payment(self):
        """Most recent payment record, for template compatibility."""
        return self.payments.order_by('-created_at').first()


class Expense(AuditModel):
    request = models.ForeignKey(TravelRequest, on_delete=models.CASCADE, related_name='expenses')
    category = models.CharField(max_length=20, choices=ExpenseCategory.choices)
    description = models.TextField()
    estimated_amount = models.DecimalField(max_digits=10, decimal_places=2)
    estimated_currency = models.CharField(max_length=3, default='EUR')
    approved_amount = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    approved_currency = models.CharField(max_length=3, blank=True, null=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.get_category_display()}: {self.estimated_amount} {self.estimated_currency}'


class StateHistory(AuditModel):
    request = models.ForeignKey(
        TravelRequest,
        on_delete=models.CASCADE,
        related_name='state_history',
    )
    from_state = models.CharField(max_length=30, blank=True, null=True)
    to_state = models.CharField(max_length=30)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['created_at']
        verbose_name_plural = 'state histories'

    def __str__(self):
        return f'#{self.request_id}: {self.from_state} → {self.to_state}'


class Comment(AuditModel):
    request = models.ForeignKey(TravelRequest, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField()

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'Comment on #{self.request_id} by {self.user.username}'


def document_upload_path(instance, filename):
    return f'uploads/{instance.request_id}/{filename}'


class Document(AuditModel):
    request = models.ForeignKey(TravelRequest, on_delete=models.CASCADE, related_name='documents')
    kind = models.CharField(max_length=20, choices=DocumentKind.choices)
    file = models.FileField(upload_to=document_upload_path)
    original_name = models.CharField(max_length=255)
    mimetype = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()} — {self.original_name}'
