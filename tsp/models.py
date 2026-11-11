from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.utils import timezone

from marvin.audit import AuditModel


class RequestState(models.TextChoices):
    """Travel request workflow states."""

    DRAFT = 'draft', 'Draft'
    SUBMITTED = 'submitted', 'Submitted'
    APPROVED = 'approved', 'Approved'
    REJECTED = 'rejected', 'Rejected'
    ACCEPTED = 'accepted', 'Accepted'
    SUBMITTED_FOR_PAYMENT = 'submitted_for_payment', 'Submitted for Payment'
    PAID = 'paid', 'Paid'
    CANCELLED = 'cancelled', 'Cancelled'


class ExpenseCategory(models.TextChoices):
    """Expense line-item categories on a travel request."""

    FLIGHT = 'flight', 'Flight'
    TRAIN = 'train', 'Train or Bus'
    LODGING = 'lodging', 'Lodging'
    # Local and public transport, and anything approved as a policy exception.
    OTHER = 'other', 'Other'


class DocumentKind(models.TextChoices):
    """Required and optional document types attached to a request."""

    RECEIPT = 'receipt', 'Receipt'
    INVOICE = 'invoice', 'Invoice'
    SIGNED_FORM = 'signed_form', 'Signed Form'


class Event(AuditModel):
    """Conference or event that requestors may apply for travel support."""

    name = models.CharField(max_length=200)
    location = models.CharField(max_length=200)
    start_date = models.DateField()
    end_date = models.DateField()
    accepting_requests = models.BooleanField(default=True)
    budget = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    budget_currency = models.CharField(max_length=3, default='EUR')

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return self.name

    @property
    def is_past(self):
        """True when the event end date is before today."""
        return self.end_date < timezone.now().date()

    def _live_requests(self):
        """Requests that still count against the budget."""
        return self.requests.exclude(
            state__in=[RequestState.REJECTED, RequestState.CANCELLED],
        ).prefetch_related('expenses')

    def approved_against_budget(self):
        """What has been approved in the budget's own currency."""
        total = Decimal('0')
        for travel_request in self._live_requests():
            for currency, amount in travel_request.totals_approved():
                if currency == self.budget_currency:
                    total += amount
        return total

    def approved_in_other_currencies(self):
        """
        Approved amounts this budget cannot draw down, per currency.

        Converting them needs a rate and a decision, so they are reported
        beside the budget rather than folded into it.
        """
        totals = {}
        for travel_request in self._live_requests():
            for currency, amount in travel_request.totals_approved():
                if currency != self.budget_currency:
                    totals[currency] = totals.get(currency, Decimal('0')) + amount
        return sorted(totals.items())

    def budget_remaining(self):
        """What is left of the budget, or None when none is set."""
        if self.budget is None:
            return None
        return self.budget - self.approved_against_budget()


class TravelRequest(AuditModel):
    """A requestor's application for travel support to a specific event."""

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
    # Set by the committee when approving: how much may be paid before the
    # event, for people who can't front the costs themselves.
    advance_amount = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    advance_currency = models.CharField(max_length=3, blank=True, null=True)
    advance_requested_at = models.DateTimeField(blank=True, null=True)
    # When finance confirmed an overpaid advance came back.
    refund_settled_at = models.DateTimeField(blank=True, null=True)
    payments = GenericRelation(
        'payments.PaymentRecord',
        content_type_field='content_type',
        object_id_field='object_id',
    )

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'Request #{self.pk} - {self.event.name}'

    @property
    def is_editable_by_requester(self):
        """True while the request is still in draft or submitted (pre-review edit)."""
        return self.state in (RequestState.DRAFT, RequestState.SUBMITTED)

    def _open_payments(self, purpose):
        return self.payments.filter(purpose=purpose).exclude(status__in=['failed', 'cancelled'])

    def can_change_amounts(self):
        """
        Approved or accepted, and no money has moved yet.

        Changing the amounts then sends the request back for review. Once an
        advance is out, the amounts it was based on have to stay put.
        """
        return (
            self.state in (RequestState.APPROVED, RequestState.ACCEPTED)
            and not self.payments.exclude(status__in=['failed', 'cancelled']).exists()
        )

    def can_take_back_from_payment(self):
        """Submitted for payment, and finance hasn't started paying it."""
        return (
            self.state == RequestState.SUBMITTED_FOR_PAYMENT
            and not self._open_payments('payment').exists()
        )

    def advance_payment(self):
        """The advance that has actually been paid, if any."""
        return self.payments.filter(
            purpose='advance', status__in=['success', 'completed'],
        ).order_by('-created_at').first()

    def can_request_advance(self):
        return (
            self.state == RequestState.ACCEPTED
            and self.advance_amount is not None
            and self.advance_requested_at is None
        )

    def awaiting_advance(self):
        """Asked for and not yet paid, nor being paid."""
        return (
            self.state == RequestState.ACCEPTED
            and self.advance_requested_at is not None
            and not self._open_payments('advance').exists()
        )

    def amount_still_to_pay(self):
        """What finance still owes per currency once any advance is taken off."""
        totals = dict(self.totals_payable())
        advance = self.advance_payment()
        if advance:
            totals[advance.currency] = totals.get(advance.currency, Decimal('0')) - advance.amount
        return sorted(totals.items())

    def amounts_to_pay(self):
        """The currencies in which finance still has something to send."""
        return [(code, amount) for code, amount in self.amount_still_to_pay() if amount > 0]

    def amount_owed_back(self):
        """
        ``(currency, amount)`` the requester owes after an advance, or None.

        Either the advance came to more than the costs, or the trip was
        cancelled or rejected after it was paid.
        """
        advance = self.advance_payment()
        if advance is None or self.refund_settled_at:
            return None
        if self.state in (RequestState.CANCELLED, RequestState.REJECTED):
            owed = advance.amount
        elif self.state in (RequestState.SUBMITTED_FOR_PAYMENT, RequestState.PAID):
            owed = -dict(self.amount_still_to_pay()).get(advance.currency, Decimal('0'))
        else:
            return None
        return (advance.currency, owed) if owed > 0 else None

    def advance_covers_costs(self):
        """Nothing left to pay because the advance already covered it."""
        return (
            self.state == RequestState.SUBMITTED_FOR_PAYMENT
            and self.advance_payment() is not None
            and all(amount <= 0 for _, amount in self.amount_still_to_pay())
        )

    # Which amount and currency field each total reads.
    AMOUNT_FIELDS = {
        'estimated': ('estimated_amount', 'estimated_currency'),
        'approved': ('approved_amount', 'approved_currency'),
        'actual': ('actual_amount', 'actual_currency'),
    }

    def totals_by_currency(self, kind):
        """
        Sum one kind of amount per currency.

        A request may mix currencies, so there is no single total. Returns a
        list of ``(currency, amount)`` sorted by currency, which templates can
        loop over.
        """
        amount_field, currency_field = self.AMOUNT_FIELDS[kind]
        totals = {}
        for expense in self.expenses.all():
            amount = getattr(expense, amount_field)
            if amount is None:
                continue
            currency = getattr(expense, currency_field) or expense.estimated_currency
            totals[currency] = totals.get(currency, 0) + amount
        return sorted(totals.items())

    def totals_estimated(self):
        """Estimated totals per currency."""
        return self.totals_by_currency('estimated')

    def totals_approved(self):
        """Approved totals per currency."""
        return self.totals_by_currency('approved')

    def totals_actual(self):
        """Reported actual totals per currency."""
        return self.totals_by_currency('actual')

    def totals_payable(self):
        """What finance owes per currency, line by line."""
        totals = {}
        for expense in self.expenses.all():
            amount = expense.payable_amount()
            if amount is None:
                continue
            currency = expense.approved_currency or expense.estimated_currency
            totals[currency] = totals.get(currency, 0) + amount
        return sorted(totals.items())

    def payment_currency(self):
        """
        The one currency this request can be paid in, or ``None`` if it mixes.

        A transfer moves one currency, so a mixed request cannot be paid as a
        single payment without someone deciding on a rate.
        """
        totals = self.totals_payable()
        return totals[0][0] if len(totals) == 1 else None

    def has_mixed_currencies(self):
        """True when the approved lines are not all in the same currency."""
        return len(self.totals_payable()) > 1
    @property
    def reference(self):
        """
        The number people quote for this request, as in TSP643.

        The same string is used on the page, in notifications and as the Wise
        transfer reference, so a comment, an email and a bank statement all
        name the request the same way.
        """
        return f'TSP{self.pk}' if self.pk else 'TSP'

    def total_estimated(self):
        """Sum of all line-item estimated amounts, ignoring currency."""
        return sum(e.estimated_amount for e in self.expenses.all())

    def total_approved(self):
        """Sum of committee-approved amounts."""
        return sum(e.approved_amount or 0 for e in self.expenses.all())

    def total_actual(self):
        """Sum of what the trip actually cost, as reported with the receipts."""
        return sum(e.actual_amount or 0 for e in self.expenses.all())

    def total_payable(self):
        """What finance pays: the lesser of approved and actual, line by line."""
        return sum(e.payable_amount() or 0 for e in self.expenses.all())

    def has_all_actual_amounts(self):
        """True when every approved line has its actual cost reported."""
        lines = [e for e in self.expenses.all() if e.approved_amount is not None]
        return bool(lines) and all(e.actual_amount is not None for e in lines)

    def expenses_needing_a_decision(self):
        """Lines whose receipt currency differs from the approved currency."""
        return [e for e in self.expenses.all() if e.has_currency_mismatch()]

    def has_all_required_documents(self):
        """True when receipt, invoice, and signed form are all uploaded."""
        kinds = set(self.documents.values_list('kind', flat=True))
        required = {DocumentKind.RECEIPT, DocumentKind.INVOICE, DocumentKind.SIGNED_FORM}
        return required.issubset(kinds)

    def missing_document_kinds(self):
        """Return human-readable labels for required documents not yet uploaded."""
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


def request_owing_money(user):
    """The first of ``user``'s requests with an advance still to pay back."""
    candidates = TravelRequest.objects.filter(
        user=user, payments__purpose='advance', refund_settled_at__isnull=True,
    ).distinct()
    return next((r for r in candidates if r.amount_owed_back()), None)


class Expense(AuditModel):
    """One estimated (and optionally approved) expense line on a travel request."""

    request = models.ForeignKey(TravelRequest, on_delete=models.CASCADE, related_name='expenses')
    category = models.CharField(max_length=20, choices=ExpenseCategory.choices)
    description = models.TextField()
    estimated_amount = models.DecimalField(max_digits=10, decimal_places=2)
    estimated_currency = models.CharField(max_length=3, default='EUR')
    approved_amount = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    approved_currency = models.CharField(max_length=3, blank=True, null=True)
    actual_amount = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    actual_currency = models.CharField(max_length=3, blank=True, null=True)
    # Fixed when the line is approved in a different currency from the one
    # requested: one unit of the approved currency in the requested currency,
    # averaged over the window ending on ``approval_rate_date``.
    approval_rate = models.DecimalField(max_digits=18, decimal_places=8, blank=True, null=True)
    approval_rate_date = models.DateField(blank=True, null=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.get_category_display()}: {self.estimated_amount} {self.estimated_currency}'

    def approved_in_requested_currency(self):
        """The approved amount converted back at the locked rate, or None."""
        if self.approved_amount is None or self.approval_rate is None:
            return None
        if self.approved_currency == self.estimated_currency:
            return None
        return (self.approved_amount * self.approval_rate).quantize(Decimal('0.01'))

    def has_currency_mismatch(self):
        """True when the receipt is in a different currency from the approval."""
        if self.actual_amount is None or not self.actual_currency:
            return False
        return self.actual_currency != self.approved_currency

    def payable_amount(self):
        """
        What this line is worth at payout: the lesser of approved and actual.

        Someone who is approved 600 and spends 400 is paid 400. Comparing two
        currencies needs a rate and a decision, so in that case the approved
        amount stands and the line is flagged for finance instead.
        """
        if self.approved_amount is None:
            return None
        if self.actual_amount is None or self.has_currency_mismatch():
            return self.approved_amount
        return min(self.approved_amount, self.actual_amount)


class ExchangeRate(AuditModel):
    """One day's reference rate: how much of ``currency`` one euro buys."""

    date = models.DateField()
    currency = models.CharField(max_length=3)
    rate = models.DecimalField(max_digits=18, decimal_places=8)
    source = models.CharField(max_length=30, default='ecb')

    class Meta:
        ordering = ['date', 'currency']
        constraints = [
            models.UniqueConstraint(fields=['date', 'currency'], name='unique_daily_rate'),
        ]

    def __str__(self):
        return f'{self.date}: 1 EUR = {self.rate} {self.currency}'


class StateHistory(AuditModel):
    """Audit log entry for each travel request state transition."""

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
    """Discussion comment on a travel request.

    Internal notes are for the committee and finance only, never the requester.
    """

    request = models.ForeignKey(TravelRequest, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField()
    internal = models.BooleanField(
        default=False,
        help_text='Only the committee and finance can see this.',
    )

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'Comment on #{self.request_id} by {self.user.username}'


def document_upload_path(instance, filename):
    """Storage path for uploaded request documents: ``uploads/{request_id}/{filename}``."""
    return f'uploads/{instance.request_id}/{filename}'


class Document(AuditModel):
    """File attachment (receipt, invoice, or signed form) on a travel request."""

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
        return f'{self.get_kind_display()} - {self.original_name}'
