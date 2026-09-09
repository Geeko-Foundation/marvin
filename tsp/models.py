from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.utils import timezone

from marvin.audit import AuditModel


class BankingMethod(models.TextChoices):
    IBAN = 'iban', 'International (IBAN)'
    US_BANK = 'us_bank', 'United States'
    INDIA_IFSC = 'india_ifsc', 'India (IFSC)'


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


class UserProfile(AuditModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    phone = models.CharField(max_length=50, blank=True)
    address_line = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    profile_completed_at = models.DateTimeField(blank=True, null=True)
    banking_method = models.CharField(
        max_length=20,
        choices=BankingMethod.choices,
        blank=True,
        null=True,
    )
    banking_currency = models.CharField(max_length=3, default='EUR')
    wise_account_id = models.PositiveIntegerField(blank=True, null=True)
    wise_recipient_verified_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['user__username']
        permissions = [
            ('submit_travel_requests', 'Can submit travel requests'),
            ('review_travel_requests', 'Can review and approve travel requests'),
            ('manage_events', 'Can manage TSP events'),
            ('view_reports', 'Can view TSP reports'),
            ('process_payments', 'Can process reimbursements'),
        ]

    def __str__(self):
        return f'{self.user.get_full_name() or self.user.username} ({self.role_label})'

    @property
    def role_label(self):
        names = list(self.user.groups.order_by('name').values_list('name', flat=True))
        return ', '.join(names) if names else 'User'

    @property
    def group_badge_class(self):
        if self.is_tsp:
            return 'badge-primary'
        if self.is_finance:
            return 'badge-secondary'
        return 'badge-accent'

    @property
    def is_requester(self) -> bool:
        from tsp.groups import GROUP_REQUESTER, user_in_group
        return user_in_group(self.user, GROUP_REQUESTER)

    @property
    def is_tsp(self) -> bool:
        from tsp.groups import GROUP_TSP, user_in_group
        return user_in_group(self.user, GROUP_TSP)

    @property
    def is_finance(self) -> bool:
        from tsp.groups import GROUP_FINANCE, user_in_group
        return user_in_group(self.user, GROUP_FINANCE)

    @property
    def is_committee(self) -> bool:
        return self.is_tsp or self.is_finance

    @property
    def requires_wise_banking(self) -> bool:
        """Requestors need Wise verification unless they are Django staff/superusers."""
        if not self.is_requester:
            return False
        return not (self.user.is_staff or self.user.is_superuser)

    @property
    def can_skip_banking(self) -> bool:
        return self.is_requester and (self.user.is_staff or self.user.is_superuser)

    def is_profile_complete(self) -> bool:
        user = self.user
        required = [
            user.first_name,
            user.email,
            self.address_line,
            self.city,
            self.country,
        ]
        return all(required)

    def is_wise_recipient_ready(self) -> bool:
        return bool(self.wise_account_id and self.wise_recipient_verified_at)

    def needs_onboarding(self) -> bool:
        if not self.is_requester:
            return False
        if not self.is_profile_complete():
            return True
        if self.requires_wise_banking and not self.is_wise_recipient_ready():
            return True
        return False

    def onboarding_step(self) -> str | None:
        """Return the next onboarding step URL name, or None if complete."""
        if not self.is_requester:
            return None
        if not self.is_profile_complete():
            return 'signup_profile'
        if self.requires_wise_banking and not self.is_wise_recipient_ready():
            return 'signup_banking'
        return None

    def banking_summary(self) -> str:
        """Non-sensitive label — account numbers live in Wise only."""
        if not self.wise_account_id:
            return 'Not configured'
        method_label = self.get_banking_method_display() if self.banking_method else 'Bank'
        return f'Wise recipient #{self.wise_account_id} ({self.banking_currency} · {method_label})'


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
        ordering = ['-changed_at']

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
