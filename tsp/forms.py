from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User

from tsp.models import (
    BankingMethod,
    Comment,
    Document,
    DocumentKind,
    Event,
    Expense,
    ExpenseCategory,
    TravelRequest,
    UserProfile,
)

CURRENCY_CHOICES = [
    ('EUR', 'EUR'), ('USD', 'USD'), ('GBP', 'GBP'), ('BRL', 'BRL'),
    ('CZK', 'CZK'), ('PLN', 'PLN'), ('AUD', 'AUD'), ('INR', 'INR'),
    ('IDR', 'IDR'), ('JPY', 'JPY'),
]

INPUT_CLASS = 'input input-bordered w-full'


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': INPUT_CLASS,
            'autocomplete': 'username',
            'autofocus': True,
        }),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': INPUT_CLASS,
            'autocomplete': 'current-password',
        }),
    )


class SignupAccountForm(forms.Form):
    """Step 1 of the sign-up wizard — create a requestor account."""

    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'username'}),
    )
    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'given-name'}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'family-name'}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'email'}),
    )
    password1 = forms.CharField(
        label='Password',
        widget=forms.PasswordInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'new-password'}),
    )
    password2 = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput(attrs={'class': INPUT_CLASS, 'autocomplete': 'new-password'}),
    )

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        return cleaned

    def save(self):
        from tsp.groups import GROUP_REQUESTER, assign_user_to_group

        user = User.objects.create_user(
            username=self.cleaned_data['username'],
            email=self.cleaned_data['email'],
            password=self.cleaned_data['password1'],
            first_name=self.cleaned_data['first_name'],
            last_name=self.cleaned_data.get('last_name', ''),
        )
        UserProfile.objects.create(user=user)
        assign_user_to_group(user, GROUP_REQUESTER, replace=True)
        return user


class ProfileForm(forms.ModelForm):
    """Personal details only — banking is managed via Wise onboarding for requestors."""

    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=True)

    class Meta:
        model = UserProfile
        fields = ['phone', 'address_line', 'city', 'state', 'country', 'postal_code']
        widgets = {
            'phone': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'address_line': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'city': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'state': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'country': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'postal_code': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user:
            self.fields['first_name'].initial = user.first_name
            self.fields['last_name'].initial = user.last_name
            self.fields['email'].initial = user.email
        for name in ('first_name', 'last_name', 'email'):
            self.fields[name].widget.attrs.update({'class': 'input input-bordered w-full'})

    def save(self, user, commit=True):
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
        super().__init__(*args, **kwargs)
        self.fields['address_line'].required = True
        self.fields['city'].required = True
        self.fields['country'].required = True

    def save(self, user, commit=True):
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
        widget=forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
    )
    bank_swift = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'input input-bordered w-full',
            'placeholder': 'Optional for SEPA / EUR',
        }),
    )
    bank_account_no = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
    )
    bank_routing_no = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
    )
    bank_ifsc = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
    )

    def __init__(self, *args, profile=None, **kwargs):
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
        cleaned = super().clean()
        method = cleaned.get('banking_method')
        if method == BankingMethod.IBAN:
            if not cleaned.get('bank_iban'):
                raise forms.ValidationError('IBAN is required for international banking.')
        elif method == BankingMethod.US_BANK:
            if not cleaned.get('bank_account_no') or not cleaned.get('bank_routing_no'):
                raise forms.ValidationError('Account number and routing number are required for US banking.')
        elif method == BankingMethod.INDIA_IFSC:
            if not cleaned.get('bank_account_no') or not cleaned.get('bank_ifsc'):
                raise forms.ValidationError('Account number and IFSC code are required for India banking.')
        elif not method:
            raise forms.ValidationError('Select a banking method.')
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


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ['name', 'location', 'start_date', 'end_date', 'accepting_requests']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'location': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'start_date': forms.DateInput(attrs={'class': 'input input-bordered w-full', 'type': 'date'}),
            'end_date': forms.DateInput(attrs={'class': 'input input-bordered w-full', 'type': 'date'}),
            'accepting_requests': forms.CheckboxInput(attrs={'class': 'checkbox'}),
        }


class TravelRequestForm(forms.ModelForm):
    class Meta:
        model = TravelRequest
        fields = ['event', 'talk_title', 'talk_url', 'description']
        widgets = {
            'event': forms.Select(attrs={'class': 'select select-bordered w-full'}),
            'talk_title': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'talk_url': forms.URLInput(attrs={'class': 'input input-bordered w-full'}),
            'description': forms.Textarea(attrs={'class': 'textarea textarea-bordered w-full', 'rows': 4}),
        }


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ['category', 'description', 'estimated_amount', 'estimated_currency']
        widgets = {
            'category': forms.Select(attrs={'class': 'select select-bordered w-full expense-category'}),
            'description': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'estimated_amount': forms.NumberInput(attrs={'class': 'input input-bordered w-full', 'step': '0.01'}),
            'estimated_currency': forms.Select(
                choices=CURRENCY_CHOICES,
                attrs={'class': 'select select-bordered w-full'},
            ),
        }


ExpenseFormSet = forms.inlineformset_factory(
    TravelRequest,
    Expense,
    form=ExpenseForm,
    extra=2,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class ApprovalForm(forms.Form):
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'textarea textarea-bordered w-full', 'rows': 3}),
    )


class RejectForm(forms.Form):
    notes = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={'class': 'textarea textarea-bordered w-full', 'rows': 3}),
    )


class AcceptForm(forms.Form):
    _checkbox_attrs = {'class': 'checkbox checkbox-warning'}

    confirm_attendance = forms.BooleanField(
        required=True,
        label='I confirm I will attend the event',
        widget=forms.CheckboxInput(attrs=_checkbox_attrs),
    )
    confirm_expenses = forms.BooleanField(
        required=True,
        label='I confirm the expense estimates are accurate',
        widget=forms.CheckboxInput(attrs=_checkbox_attrs),
    )
    confirm_coc = forms.BooleanField(
        required=True,
        label='I agree to abide by the event Code of Conduct',
        widget=forms.CheckboxInput(attrs=_checkbox_attrs),
    )
    confirm_rules = forms.BooleanField(
        required=True,
        label='I agree to the TSP program rules',
        widget=forms.CheckboxInput(attrs=_checkbox_attrs),
    )


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ['body']
        widgets = {
            'body': forms.Textarea(attrs={
                'class': 'textarea textarea-bordered w-full',
                'rows': 3,
                'placeholder': 'Add a comment…',
            }),
        }


class DocumentUploadForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ['kind', 'file']
        widgets = {
            'kind': forms.Select(attrs={'class': 'select select-bordered w-full'}),
            'file': forms.FileInput(attrs={'class': 'file-input file-input-bordered w-full'}),
        }


class ReportFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'input input-bordered', 'type': 'date'}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'input input-bordered', 'type': 'date'}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=[
            ('', 'All statuses'),
            ('cancelled', 'Cancelled'),
            ('rejected', 'Rejected'),
            ('accepted', 'Accepted'),
            ('submitted_for_payment', 'Submitted for Payment'),
            ('paid', 'Paid'),
        ],
        widget=forms.Select(attrs={'class': 'select select-bordered'}),
    )


class SearchForm(forms.Form):
    q = forms.CharField(
        required=False,
        max_length=200,
        label='Search',
        widget=forms.TextInput(attrs={
            'class': INPUT_CLASS,
            'placeholder': 'Events, locations, users…',
            'autocomplete': 'off',
        }),
    )
