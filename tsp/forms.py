import os
from decimal import Decimal

from django import forms
from django.core.validators import MinValueValidator

from marvin.validators import NAME, line, restrict, text
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User

from profile.models import UserProfile
from tsp.models import (
    Comment,
    Document,
    DocumentKind,
    Event,
    Expense,
    ExpenseCategory,
    RequestState,
    TravelRequest,
)

CURRENCY_CHOICES = [
    ('EUR', 'EUR'), ('USD', 'USD'), ('GBP', 'GBP'), ('BRL', 'BRL'),
    ('CZK', 'CZK'), ('PLN', 'PLN'), ('AUD', 'AUD'), ('INR', 'INR'),
    ('IDR', 'IDR'), ('JPY', 'JPY'),
]

INPUT_CLASS = 'input input-bordered w-full'


class LoginForm(AuthenticationForm):
    """Username/password login with Tailwind-styled fields."""

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
    """Step 1 of the sign-up wizard - create a requestor account."""

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
        """Reject usernames that already exist (case-insensitive)."""
        username = self.cleaned_data['username']
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean_email(self):
        """Reject emails already registered to another account."""
        email = self.cleaned_data['email']
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean(self):
        """Ensure password confirmation matches."""
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        return cleaned

    def save(self):
        """Create ``User``, ``UserProfile``, and assign the Requester group."""
        from profile.groups import GROUP_REQUESTER, assign_user_to_group

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


class EventForm(forms.ModelForm):
    """Create or edit a TSP event."""

    class Meta:
        model = Event
        fields = ['name', 'location', 'start_date', 'end_date', 'accepting_requests',
                  'budget', 'budget_currency', 'url', 'description',
                  'requests_open_until', 'reimbursements_open_until',
                  'visa_letters_offered']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'location': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'start_date': forms.DateInput(attrs={'class': 'input input-bordered w-full', 'type': 'date'}),
            'end_date': forms.DateInput(attrs={'class': 'input input-bordered w-full', 'type': 'date'}),
            'accepting_requests': forms.CheckboxInput(attrs={'class': 'checkbox'}),
            'budget': forms.NumberInput(attrs={
                'class': 'input input-bordered w-full', 'step': '0.01', 'min': '0',
                'placeholder': 'Leave blank for no limit',
            }),
            'budget_currency': forms.Select(
                choices=CURRENCY_CHOICES,
                attrs={'class': 'select select-bordered w-full'},
            ),
            'url': forms.URLInput(attrs={'class': 'input input-bordered w-full'}),
            'description': forms.Textarea(attrs={
                'class': 'textarea textarea-bordered w-full', 'rows': 3,
            }),
            'requests_open_until': forms.DateInput(attrs={
                'class': 'input input-bordered w-full', 'type': 'date',
            }),
            'reimbursements_open_until': forms.DateInput(attrs={
                'class': 'input input-bordered w-full', 'type': 'date',
            }),
            'visa_letters_offered': forms.CheckboxInput(attrs={'class': 'checkbox'}),
        }

    def __init__(self, *args, **kwargs):
        """The currency only matters once a budget is set, so let it default."""
        super().__init__(*args, **kwargs)
        self.fields['budget_currency'].required = False
        self.fields['budget'].validators.append(MinValueValidator(Decimal('0')))

    def clean_budget_currency(self):
        """Fall back to the model default when nothing was chosen."""
        return self.cleaned_data.get('budget_currency') or 'EUR'

    def clean(self):
        """An event cannot end before it starts."""
        cleaned = super().clean()
        start, end = cleaned.get('start_date'), cleaned.get('end_date')
        if start and end and end < start:
            self.add_error('end_date', 'The end date cannot be before the start date.')
        return cleaned


class TravelRequestForm(forms.ModelForm):
    """Core travel request fields (event, talk info, description)."""

    class Meta:
        model = TravelRequest
        fields = ['event', 'talk_title', 'talk_url', 'description']
        widgets = {
            'event': forms.Select(attrs={'class': 'select select-bordered w-full'}),
            'talk_title': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'talk_url': forms.URLInput(attrs={'class': 'input input-bordered w-full'}),
            'description': forms.Textarea(attrs={'class': 'textarea textarea-bordered w-full', 'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        """
        Offer only events still taking requests.

        A request already filed against an event that has since closed keeps
        that event, so editing it does not force a different one.
        """
        super().__init__(*args, **kwargs)
        events = Event.open_for_requests()
        if self.instance.pk and self.instance.event_id:
            events = events | Event.objects.filter(pk=self.instance.event_id)
        self.fields['event'].queryset = events.distinct().order_by('start_date')


class ExpenseForm(forms.ModelForm):
    """Single expense line item on a travel request."""

    class Meta:
        model = Expense
        fields = ['category', 'description', 'estimated_amount', 'estimated_currency']
        widgets = {
            'category': forms.Select(attrs={'class': 'select select-bordered w-full expense-category'}),
            'description': forms.TextInput(attrs={'class': 'input input-bordered w-full'}),
            'estimated_amount': forms.NumberInput(attrs={'class': 'input input-bordered w-full', 'step': '0.01', 'min': '0.01'}),
            'estimated_currency': forms.Select(
                choices=CURRENCY_CHOICES,
                attrs={'class': 'select select-bordered w-full'},
            ),
        }

    def __init__(self, *args, **kwargs):
        """An estimate has to be more than zero, whatever the browser allowed."""
        super().__init__(*args, **kwargs)
        self.fields['category'].choices = [('', 'Choose')] + [
            c for c in self.fields['category'].choices if c[0]
        ]
        self.fields['estimated_amount'].validators.append(MinValueValidator(Decimal('0.01')))


ExpenseFormSet = forms.inlineformset_factory(
    TravelRequest,
    Expense,
    form=ExpenseForm,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class ExpenseActualForm(forms.ModelForm):
    """What a line actually cost, reported with the receipts."""

    class Meta:
        model = Expense
        fields = ['actual_amount', 'actual_currency']
        widgets = {
            'actual_amount': forms.NumberInput(
                attrs={'class': 'input input-bordered w-full', 'step': '0.01', 'min': '0'},
            ),
            'actual_currency': forms.Select(
                choices=CURRENCY_CHOICES,
                attrs={'class': 'select select-bordered w-full'},
            ),
        }

    def __init__(self, *args, **kwargs):
        """Default the currency to the one the line was approved in."""
        super().__init__(*args, **kwargs)
        self.fields['actual_amount'].required = True
        self.fields['actual_amount'].validators.append(MinValueValidator(Decimal('0')))
        approved = self.instance.approved_currency if self.instance else None
        if approved and not self.instance.actual_currency:
            self.fields['actual_currency'].initial = approved


ExpenseActualFormSet = forms.inlineformset_factory(
    TravelRequest,
    Expense,
    form=ExpenseActualForm,
    extra=0,
    can_delete=False,
)


class ApprovalForm(forms.Form):
    """Optional committee notes when approving a request."""

    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'textarea textarea-bordered w-full', 'rows': 3}),
    )


class RejectForm(forms.Form):
    """Required rejection notes from the TSP committee."""

    notes = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={'class': 'textarea textarea-bordered w-full', 'rows': 3}),
    )


class AcceptForm(forms.Form):
    """Required confirmations before a requestor accepts an approved request."""

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
    """Add a comment to a travel request thread."""

    class Meta:
        model = Comment
        fields = ['body', 'internal']
        widgets = {
            'body': forms.Textarea(attrs={
                'class': 'textarea textarea-bordered w-full',
                'rows': 3,
                'placeholder': 'Add a comment...',
            }),
            'internal': forms.CheckboxInput(attrs={'class': 'checkbox checkbox-sm checkbox-warning'}),
        }

    def __init__(self, *args, can_write_internal=False, **kwargs):
        super().__init__(*args, **kwargs)
        # Dropping the field, rather than hiding it, means a posted value from
        # someone who may not write internal notes is simply ignored.
        if not can_write_internal:
            del self.fields['internal']


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024

# Receipts, invoices and signed forms are PDFs or photos. The browser chooses
# the content type, so check the opening bytes against the extension too.
DOCUMENT_SIGNATURES = {
    '.pdf': (b'%PDF-',),
    '.png': (b'\x89PNG\r\n\x1a\n',),
    '.jpg': (b'\xff\xd8\xff',),
    '.jpeg': (b'\xff\xd8\xff',),
}


class DocumentUploadForm(forms.ModelForm):
    """Upload a receipt, invoice, or signed form attachment."""

    class Meta:
        model = Document
        fields = ['kind', 'file']
        widgets = {
            'kind': forms.Select(attrs={'class': 'select select-bordered w-full'}),
            'file': forms.FileInput(attrs={
                'class': 'file-input file-input-bordered w-full',
                'accept': '.pdf,.png,.jpg,.jpeg',
            }),
        }

    def clean_file(self):
        uploaded = self.cleaned_data.get('file')
        if not uploaded:
            return uploaded

        if uploaded.size > MAX_DOCUMENT_BYTES:
            raise forms.ValidationError('Files must be 10 MB or smaller.')

        extension = os.path.splitext(uploaded.name)[1].lower()
        signatures = DOCUMENT_SIGNATURES.get(extension)
        if not signatures:
            raise forms.ValidationError('Upload a PDF, PNG or JPEG.')

        head = uploaded.read(8)
        uploaded.seek(0)
        if not head.startswith(signatures):
            raise forms.ValidationError("That file's contents don't match its extension.")

        return uploaded


class ReportFilterForm(forms.Form):
    """Filter TSP reports by event date range and request status."""

    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'input input-bordered', 'type': 'date'}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'input input-bordered', 'type': 'date'}),
    )
    # Every state, rather than the five that used to be listed: draft,
    # submitted and approved could not be filtered for at all.
    status = forms.ChoiceField(
        required=False,
        choices=[('', 'All statuses')] + list(RequestState.choices),
        widget=forms.Select(attrs={'class': 'select select-bordered'}),
    )
    q = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': 'input input-bordered',
            'placeholder': 'Requester or event',
        }),
    )


class SearchForm(forms.Form):
    """Global search query for events and (staff-only) users."""

    q = forms.CharField(
        required=False,
        max_length=200,
        label='Search',
        widget=forms.TextInput(attrs={
            'class': INPUT_CLASS,
            'placeholder': 'Events, locations, users...',
            'autocomplete': 'off',
        }),
    )


# What each free-text field may hold. See marvin/validators.py.
restrict(SignupAccountForm, first_name=NAME, last_name=NAME)
restrict(TravelRequestForm, talk_title=line(300), description=text(5000))
restrict(ExpenseForm, description=line(300))
restrict(EventForm, name=line(200), location=line(200), description=text(5000))
restrict(CommentForm, body=text(5000))
restrict(ApprovalForm, notes=text(2000))
restrict(RejectForm, notes=text(2000))
