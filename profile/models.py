from django.conf import settings
from django.db import models

from marvin.audit import AuditModel
from profile.countries import STATE_CODES, normalise_country


class BankingMethod(models.TextChoices):
    """Supported payout rail identifiers stored on ``UserProfile``."""

    IBAN = 'iban', 'International (IBAN)'
    US_BANK = 'us_bank', 'United States'
    INDIA_IFSC = 'india_ifsc', 'India (IFSC)'
    AUSTRALIA = 'australia', 'Australia (BSB)'
    BRAZIL = 'brazil', 'Brazil (CPF)'
    INDONESIA = 'indonesia', 'Indonesia'
    JAPAN = 'japan', 'Japan'


class UserProfile(AuditModel):
    """Extended user record: contact info, role flags, Wise recipient, and push prefs."""

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
    push_notifications_enabled = models.BooleanField(
        default=False,
        help_text='Receive browser push notifications for TSP activity.',
    )

    class Meta:
        db_table = 'tsp_userprofile'
        ordering = ['user__username']
        permissions = [
            ('submit_travel_requests', 'Can submit travel requests'),
            ('review_travel_requests', 'Can review and approve travel requests'),
            ('manage_events', 'Can manage TSP events'),
            ('view_reports', 'Can view TSP reports'),
            ('process_payments', 'Can process reimbursements'),
        ]

    def __str__(self):
        """Short label for admin lists and debugging."""
        return f'{self.user.get_full_name() or self.user.username} ({self.role_label})'

    @property
    def role_label(self):
        """Comma-separated Django group names, or ``User`` when unassigned."""
        names = list(self.user.groups.order_by('name').values_list('name', flat=True))
        return ', '.join(names) if names else 'User'

    @property
    def group_badge_class(self):
        """Tailwind/DaisyUI badge CSS class for the user's primary committee role."""
        if self.is_tsp:
            return 'badge-primary'
        if self.is_finance:
            return 'badge-secondary'
        return 'badge-accent'

    @property
    def is_requester(self) -> bool:
        """True when the user belongs to the Requester group."""
        from profile.groups import GROUP_REQUESTER, user_in_group
        return user_in_group(self.user, GROUP_REQUESTER)

    @property
    def is_tsp(self) -> bool:
        """True when the user belongs to the TSP Committee group."""
        from profile.groups import GROUP_TSP, user_in_group
        return user_in_group(self.user, GROUP_TSP)

    @property
    def is_finance(self) -> bool:
        """True when the user belongs to the Finance group."""
        from profile.groups import GROUP_FINANCE, user_in_group
        return user_in_group(self.user, GROUP_FINANCE)

    @property
    def is_committee(self) -> bool:
        """True when the user is TSP or Finance staff (not a plain requester)."""
        return self.is_tsp or self.is_finance

    @property
    def requires_wise_banking(self) -> bool:
        """Requestors (except staff/superuser) must complete Wise recipient verification."""
        if not self.is_requester:
            return False
        return not (self.user.is_staff or self.user.is_superuser)

    @property
    def can_skip_banking(self) -> bool:
        """Staff and superuser requestors may bypass Wise onboarding."""
        return self.is_requester and (self.user.is_staff or self.user.is_superuser)

    def is_profile_complete(self) -> bool:
        """True when required personal and address fields are filled."""
        user = self.user
        required = [
            user.first_name,
            user.email,
            self.address_line,
            self.city,
            self.country,
            # Wise wants a post code on every corridor, and a state for US and
            # Australian addresses, or it refuses to create the payee.
            self.postal_code,
        ]
        if normalise_country(self.country) in STATE_CODES:
            required.append(self.state)
        return all(required)

    def is_wise_recipient_ready(self) -> bool:
        """True when a verified Wise account ID is linked on the profile."""
        return bool(self.wise_account_id and self.wise_recipient_verified_at)

    def needs_onboarding(self) -> bool:
        """True when a requestor still owes profile or Wise banking setup."""
        if not self.is_requester:
            return False
        if not self.is_profile_complete():
            return True
        if self.requires_wise_banking and not self.is_wise_recipient_ready():
            return True
        return False

    def onboarding_step(self) -> str | None:
        """
        Return the next onboarding step key, or ``None`` when complete.

        Steps: ``signup_profile`` → ``signup_banking``.
        """
        if not self.is_requester:
            return None
        if not self.is_profile_complete():
            return 'signup_profile'
        if self.requires_wise_banking and not self.is_wise_recipient_ready():
            return 'signup_banking'
        return None

    def banking_summary(self) -> str:
        """Human-readable Wise recipient summary for templates and status checks."""
        if not self.wise_account_id:
            return 'Not configured'
        method_label = self.get_banking_method_display() if self.banking_method else 'Bank'
        return f'Wise recipient #{self.wise_account_id} ({self.banking_currency} · {method_label})'
