from django import forms
from django.contrib.auth.models import User

from marvin.validators import BANK, NAME, PHONE, POSTCODE, line, restrict
from profile.countries import COUNTRIES, STATE_CODES, normalise_country, state_code
from profile.models import BankingMethod, UserProfile
from profile.services.wise_recipient import (
    ACCOUNT_TYPES,
    METHOD_FOR_CURRENCY,
    currency_for_method,
)

CURRENCY_CHOICES = [
    ('EUR', 'EUR'), ('USD', 'USD'), ('GBP', 'GBP'), ('BRL', 'BRL'),
    ('CZK', 'CZK'), ('PLN', 'PLN'), ('AUD', 'AUD'), ('INR', 'INR'),
    ('IDR', 'IDR'), ('JPY', 'JPY'),
]

INPUT_CLASS = 'input input-bordered w-full'

FIELD_LABELS = {
    'bank_iban': 'IBAN',
    'bank_account_no': 'account number',
    'bank_routing_no': 'routing number',
    'bank_ifsc': 'IFSC code',
    'bank_bsb': 'BSB code',
    'bank_code': 'bank',
    'bank_branch': 'branch code',
    'bank_japan_branch': 'branch',
    'bank_cpf': 'CPF',
    'bank_account_type': 'account type',
}

REQUIRED_BANK_FIELDS = {
    BankingMethod.IBAN: ('bank_iban',),
    BankingMethod.US_BANK: ('bank_account_no', 'bank_routing_no', 'bank_account_type'),
    BankingMethod.INDIA_IFSC: ('bank_account_no', 'bank_ifsc'),
    BankingMethod.AUSTRALIA: ('bank_account_no', 'bank_bsb'),
    BankingMethod.BRAZIL: ('bank_account_no', 'bank_code', 'bank_branch', 'bank_cpf',
                           'bank_account_type'),
    BankingMethod.INDONESIA: ('bank_account_no', 'bank_code'),
    BankingMethod.JAPAN: ('bank_account_no', 'bank_code', 'bank_japan_branch',
                          'bank_account_type'),
}


# Bank codes and numbers: no autocorrect, capitals or spellcheck, which would
# change what someone types on a phone keyboard.
CODE_FIELD = {'autocorrect': 'off', 'autocapitalize': 'none', 'spellcheck': 'false'}


class ProfileForm(forms.ModelForm):
    """Personal details only - banking is managed via Wise onboarding for requestors."""

    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=True)
    # Asked for only when the email changes, so someone at an unlocked screen
    # cannot move the account to their own address and reset the password.
    current_password = forms.CharField(
        required=False, strip=False, label='Current password',
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}),
        help_text='Needed only to change your email.',
    )

    class Meta:
        model = UserProfile
        fields = ['phone', 'country']
        widgets = {
            'phone': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'country': forms.Select(
                attrs={'class': 'select select-bordered w-full'},
                choices=[('', 'Select your country')] + list(COUNTRIES),
            ),
        }

    def __init__(self, *args, **kwargs):
        """Pre-fill name and email from the linked ``User``; apply Tailwind input classes."""
        user = kwargs.pop('user', None)
        self.user = user
        super().__init__(*args, **kwargs)
        if user:
            self.fields['first_name'].initial = user.first_name
            self.fields['last_name'].initial = user.last_name
            self.fields['email'].initial = user.email
        for name in ('first_name', 'last_name', 'email', 'current_password'):
            self.fields[name].widget.attrs.update({'class': INPUT_CLASS})

    def clean_email(self):
        """Reject an address another account already uses, as sign-up does."""
        email = self.cleaned_data['email']
        others = User.objects.filter(email__iexact=email)
        if self.user:
            others = others.exclude(pk=self.user.pk)
        if others.exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    @property
    def new_email(self):
        """The address typed in, when it differs from the one on the account."""
        email = self.cleaned_data.get('email', '')
        return email if self.user and email.lower() != self.user.email.lower() else None

    def clean(self):
        """A new email needs the current password."""
        cleaned = super().clean()
        if self.new_email and not self.user.check_password(cleaned.get('current_password') or ''):
            self.add_error('current_password', 'Enter your current password to change your email.')
        return cleaned

    def save(self, user, commit=True):
        """
        Persist profile fields and sync the ``User`` name.

        A new email address is not saved here: it waits until the link sent to
        it is opened, see ``send_email_change_link``.
        """
        profile = super().save(commit=False)
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.save(update_fields=['first_name', 'last_name'])
        if commit:
            profile.save()
        return profile


class OnboardingPersonalForm(ProfileForm):
    """First-time requestor profile, where the country is required."""

    def __init__(self, *args, **kwargs):
        """Require the country, which decides how someone can be paid."""
        super().__init__(*args, **kwargs)
        self.fields['country'].required = True

    def save(self, user, commit=True):
        """Save personal details and record onboarding completion timestamp."""
        from django.utils import timezone

        profile = super().save(user, commit=False)
        profile.profile_completed_at = timezone.now()
        if commit:
            profile.save()
        return profile


class WiseRecipientForm(forms.Form):
    """Banking details sent to Wise only - never stored in Marvin."""

    banking_method = forms.ChoiceField(
        choices=BankingMethod.choices,
        widget=forms.Select(attrs={'class': 'select select-bordered w-full'}),
    )
    banking_currency = forms.ChoiceField(
        choices=CURRENCY_CHOICES,
        widget=forms.Select(attrs={'class': 'select select-bordered w-full'}),
    )
    bank_iban = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={**CODE_FIELD, 'class': INPUT_CLASS}),
    )
    bank_swift = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            **CODE_FIELD,
            'class': INPUT_CLASS,
            'placeholder': 'Optional for SEPA / EUR',
        }),
    )
    bank_account_no = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={**CODE_FIELD, 'inputmode': 'numeric', 'class': INPUT_CLASS}),
    )
    bank_routing_no = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={**CODE_FIELD, 'inputmode': 'numeric', 'class': INPUT_CLASS}),
    )
    bank_ifsc = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={**CODE_FIELD, 'class': INPUT_CLASS}),
    )
    bank_bsb = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={**CODE_FIELD, 'class': INPUT_CLASS}),
    )
    bank_cpf = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    # Brazil, Indonesia and Japan pick a bank from Wise's own list, which is
    # far too long to ship here, so these selects are filled in by the page.
    bank_code = forms.CharField(
        required=False,
        widget=forms.Select(attrs={'class': 'select select-bordered w-full'}),
    )
    bank_branch = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    # Japan is the one country where Wise lists branches, per chosen bank.
    bank_japan_branch = forms.CharField(
        required=False,
        widget=forms.Select(attrs={'class': 'select select-bordered w-full'}),
    )
    # Required for US, Brazil and Japan, with different values for each.
    bank_account_type = forms.ChoiceField(
        required=False,
        choices=[('CHECKING', 'Checking'), ('SAVINGS', 'Savings'),
                 ('CURRENT', 'Current'), ('PAYMENT', 'Payment')],
        widget=forms.Select(attrs={'class': 'select select-bordered w-full'}),
    )

    # The address goes to Wise with the bank details, which needs it to create
    # the payee, and like them it is never saved here.
    address_line = forms.CharField(label='Street address', widget=forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'street-address'}))
    city = forms.CharField(widget=forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'address-level2'}))
    state = forms.CharField(required=False, label='State / province', widget=forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'address-level1'}))
    postal_code = forms.CharField(label='Postal code', widget=forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'postal-code'}))

    # Asked for only when there is already a payout account, so someone at an
    # unlocked screen cannot send the next payout to their own account.
    current_password = forms.CharField(
        required=False,
        label='Current password',
        widget=forms.PasswordInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'current-password'}),
    )

    def __init__(self, *args, profile=None, **kwargs):
        """Pre-select banking method and currency from an existing profile when present."""
        super().__init__(*args, **kwargs)
        self.profile = profile
        if profile:
            if profile.banking_method:
                self.fields['banking_method'].initial = profile.banking_method
            if profile.banking_currency:
                self.fields['banking_currency'].initial = profile.banking_currency
        self.fields['bank_swift'].help_text = (
            'Optional for most SEPA transfers, where the IBAN alone is usually enough.'
        )

    def clean(self):
        """Validate that required bank fields are present for the selected method."""
        cleaned = super().clean()
        if (self.profile and self.profile.has_had_payout_account()
                and not self.profile.user.check_password(cleaned.get('current_password') or '')):
            self.add_error('current_password', 'Enter your current password to change where you are paid.')
        # Wise refuses US and Australian payees without a state it recognises.
        country = normalise_country(self.profile.country if self.profile else '')
        if country in STATE_CODES and not state_code(country, cleaned.get('state')):
            example = 'Oregon' if country == 'US' else 'Victoria'
            self.add_error('state', f'Enter a state for this country, for example "{example}".')
        method = cleaned.get('banking_method')
        if not method:
            raise forms.ValidationError('Select a banking method.')

        currency = cleaned.get('banking_currency')
        # Wise ties most of these rails to one currency and answers "Invalid
        # currency for this account type" otherwise, which tells nobody much.
        wanted = currency_for_method(method)
        if wanted and currency and currency != wanted:
            raise forms.ValidationError(
                f'{BankingMethod(method).label} accounts are paid in {wanted}, not {currency}.'
            )
        owner = METHOD_FOR_CURRENCY.get(currency)
        if owner and method != owner:
            raise forms.ValidationError(
                f'{currency} payouts need the "{BankingMethod(owner).label}" method.'
            )

        missing = [name for name in REQUIRED_BANK_FIELDS.get(method, ()) if not cleaned.get(name)]
        if missing:
            raise forms.ValidationError(
                f'{BankingMethod(method).label} banking needs: '
                + ', '.join(FIELD_LABELS[name] for name in missing) + '.'
            )

        allowed = ACCOUNT_TYPES.get(method)
        account_type = cleaned.get('bank_account_type')
        if allowed and account_type and account_type not in allowed:
            names = [t.lower() for t in allowed]
            readable = f'{", ".join(names[:-1])} or {names[-1]}'
            raise forms.ValidationError(
                f'{BankingMethod(method).label} accounts must be {readable}.'
            )
        return cleaned


class StaffBankingForm(forms.ModelForm):
    """Non-sensitive banking metadata for staff profiles (no account numbers stored)."""

    class Meta:
        model = UserProfile
        fields = ['banking_method', 'banking_currency']
        widgets = {
            'banking_method': forms.Select(attrs={'class': 'select select-bordered w-full'}),
            'banking_currency': forms.Select(
                choices=CURRENCY_CHOICES,
                attrs={'class': 'select select-bordered w-full'},
            ),
        }


# What each free-text field may hold. See marvin/validators.py.
for _form in (ProfileForm, OnboardingPersonalForm):
    restrict(_form, first_name=NAME, last_name=NAME, phone=PHONE, country=line(100))
restrict(
    WiseRecipientForm, postal_code=POSTCODE,
    address_line=line(200), city=line(100), state=line(100),
)
restrict(WiseRecipientForm, **{
    name: BANK for name in (
        'bank_iban', 'bank_swift', 'bank_account_no', 'bank_routing_no', 'bank_ifsc',
        'bank_bsb', 'bank_cpf', 'bank_code', 'bank_branch', 'bank_japan_branch',
    )
})
