from django import forms
from django.contrib.auth.models import User

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


class ProfileForm(forms.ModelForm):
    """Personal details only — banking is managed via Wise onboarding for requestors."""

    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=True)

    class Meta:
        model = UserProfile
        fields = [
            'phone',
            'address_line',
            'city',
            'state',
            'country',
            'postal_code',
            'push_notifications_enabled',
        ]
        widgets = {
            'phone': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'address_line': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'city': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'state': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'country': forms.Select(
                attrs={'class': 'select select-bordered w-full'},
                choices=[('', 'Select your country')] + list(COUNTRIES),
            ),
            'postal_code': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'push_notifications_enabled': forms.CheckboxInput(attrs={'class': 'checkbox'}),
        }

    def __init__(self, *args, **kwargs):
        """Pre-fill name and email from the linked ``User``; apply Tailwind input classes."""
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user:
            self.fields['first_name'].initial = user.first_name
            self.fields['last_name'].initial = user.last_name
            self.fields['email'].initial = user.email
        for name in ('first_name', 'last_name', 'email'):
            self.fields[name].widget.attrs.update({'class': INPUT_CLASS})

    def clean(self):
        """Wise refuses US and Australian payees without a state it recognises."""
        cleaned = super().clean()
        country = normalise_country(cleaned.get('country'))
        if country in STATE_CODES and not state_code(country, cleaned.get('state')):
            example = 'Oregon' if country == 'US' else 'Victoria'
            self.add_error('state', f'Enter a state for this country, for example "{example}".')
        return cleaned

    def save(self, user, commit=True):
        """Persist profile fields and sync ``User`` first name, last name, and email."""
        profile = super().save(commit=False)
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.email = self.cleaned_data['email']
        user.save()
        if commit:
            profile.save()
        return profile


class OnboardingPersonalForm(ProfileForm):
    """First-time requestor profile — all address fields required."""

    def __init__(self, *args, **kwargs):
        """Require address fields that are optional on the standard profile form."""
        super().__init__(*args, **kwargs)
        self.fields['address_line'].required = True
        self.fields['city'].required = True
        self.fields['country'].required = True
        self.fields['postal_code'].required = True

    def save(self, user, commit=True):
        """Save personal details and record onboarding completion timestamp."""
        from django.utils import timezone

        profile = super().save(user, commit=False)
        profile.profile_completed_at = timezone.now()
        if commit:
            profile.save()
        return profile


class WiseRecipientForm(forms.Form):
    """Banking details sent to Wise only — never stored in Marvin."""

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
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    bank_swift = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': INPUT_CLASS,
            'placeholder': 'Optional for SEPA / EUR',
        }),
    )
    bank_account_no = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    bank_routing_no = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    bank_ifsc = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    bank_bsb = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
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

    def __init__(self, *args, profile=None, **kwargs):
        """Pre-select banking method and currency from an existing profile when present."""
        super().__init__(*args, **kwargs)
        if profile:
            if profile.banking_method:
                self.fields['banking_method'].initial = profile.banking_method
            if profile.banking_currency:
                self.fields['banking_currency'].initial = profile.banking_currency
        self.fields['bank_swift'].help_text = (
            'Optional for many SEPA transfers — IBAN alone is usually enough within the EU.'
        )

    def clean(self):
        """Validate that required bank fields are present for the selected method."""
        cleaned = super().clean()
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
