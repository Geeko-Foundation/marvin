"""TSP views: auth, onboarding, events, travel requests, reports, and search."""

import csv
import re
import time
from decimal import Decimal, InvalidOperation
from functools import partial

import requests

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.cache import caches
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, ProtectedError, Q, Sum, Value
from django.db.models.functions import Coalesce, NullIf
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.http import require_POST
from django_tables2 import RequestConfig

from marvin.validators import spreadsheet_safe
from payments.models import PaymentRecord
from payments.services import PaymentService
from profile.audit_log import AuditLogEntry
from profile.audit_log import record as audit
from profile.decorators import get_profile
from profile.forms import OnboardingPersonalForm, WiseRecipientForm
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP
from profile.views import ask_to_confirm_email
from profile.services.wise_banking_options import bank_choices, branch_choices
from profile.services.wise_recipient import (
    RETURN_TO_SESSION_KEY,
    banking_session_payload,
    check_wise_recipient,
)
from profile.services.wise_verification import (
    SESSION_KEY,
    VERIFICATION_STEPS,
    WiseVerificationError,
    init_verification_state,
    run_verification_step,
    verification_state,
)
from tsp.accounts import activation_token, send_activation_email
from tsp.decorators import group_required, login_required, staff_or_owner
from tsp.forms import (
    AcceptForm,
    ApprovalForm,
    CommentForm,
    DocumentUploadForm,
    EventBudgetForm,
    EventForm,
    EventProposalForm,
    ExpenseActualFormSet,
    ExpenseFormSet,
    LoginForm,
    RejectForm,
    ReportFilterForm,
    SearchForm,
    SignupAccountForm,
    TravelRequestForm,
)
from tsp.models import (
    SETTLED_PAYMENT_STATUSES,
    Document,
    Event,
    Expense,
    RequestState,
    TravelRequest,
    request_owing_money,
)
from tsp.payables import TravelRequestPayableAdapter
from tsp.services import exchange_rates
from tsp.services.state_machine import (
    StateTransitionError,
    create_request_with_history,
    transition_request,
)
from tsp.tables import EventTable, UserTable


def _is_safe_redirect(request, url):
    """Only back into this site, never on to a link someone planted."""
    return bool(url) and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    )


# Ten wrong passwords from one address lock that address out of the account.
# A far higher limit across every address still stops a spread-out attack,
# without letting one person lock the real owner out after ten guesses.
LOGIN_FAILURE_LIMIT = 10
LOGIN_ACCOUNT_FAILURE_LIMIT = 100
LOGIN_LOCK_SECONDS = 15 * 60

RESEND_LIMIT = 3
RESEND_WINDOW_SECONDS = 24 * 60 * 60

# Each try creates a payee in Wise, so a few a day covers typing mistakes.
BANK_SETUP_LIMIT = 5
BANK_SETUP_WINDOW_SECONDS = 24 * 60 * 60
BANK_SETUP_REFUSED = 'Too many attempts to set up bank details today. Try again tomorrow, or contact the TSP team.'


# Counters live in a cache of their own, so filling the shared one cannot
# reset them. shortcut: each server process keeps its own counts, so N
# workers allow N times every limit; use a shared cache if that matters.
limits = caches['limits']


def _under_limit(key, limit, window_seconds):
    """Count one more use of ``key`` and say whether it is still within ``limit``."""
    limits.add(key, 0, window_seconds)
    return limits.incr(key) <= limit


def _counts_as_bank_attempt(form):
    """
    A bank setup try, or a wrong current password typed into one.

    Wrong passwords count too, so the form cannot be used to guess the
    password of whoever left their session open.
    """
    return form.is_valid() or bool(form.data.get('current_password') and 'current_password' in form.errors)


def _client_address(request):
    """
    The visitor's address. Behind a proxy that is the last one it appended.

    shortcut: assumes a single proxy in front of the app, as on Railway; with
    more, the last entry is one of them and visitors share one count.
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded and settings.SECURE_PROXY_SSL_HEADER:
        return forwarded.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR', '')


def login_view(request):
    """Authenticate with username/password; honor ``next`` redirect when present."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST':
        username = request.POST.get('username', '').strip().lower()
        account_key = 'login-failures:' + username
        failures_key = f'{account_key}:{_client_address(request)}'
        if (limits.get(failures_key, 0) >= LOGIN_FAILURE_LIMIT
                or limits.get(account_key, 0) >= LOGIN_ACCOUNT_FAILURE_LIMIT):
            form.add_error(None, 'Too many failed sign-in attempts for this account. Try again in 15 minutes.')
        elif form.is_valid():
            limits.delete(failures_key)
            login(request, form.get_user())
            next_url = request.POST.get('next') or request.GET.get('next')
            if _is_safe_redirect(request, next_url):
                return redirect(next_url)
            return redirect(settings.LOGIN_REDIRECT_URL)
        elif User.objects.filter(username__iexact=username).exists():
            # Names with no account are not counted, so made-up ones cannot
            # fill the counters and push a real account's count out.
            for key in (failures_key, account_key):
                limits.add(key, 0, LOGIN_LOCK_SECONDS)
                limits.incr(key)


    return render(request, 'tsp/login.html', {
        'form': form,
        'next': request.GET.get('next', ''),
    })


@login_required
def logout_view(request):
    """End the session and redirect to the login page."""
    logout(request)
    return redirect('login')


def _mark_profile_completed(profile):
    """Stamp ``profile_completed_at`` when required fields are first satisfied."""
    if profile.is_profile_complete() and not profile.profile_completed_at:
        profile.profile_completed_at = timezone.now()
        profile.save(update_fields=['profile_completed_at'])


def _signup_redirect_for_user(user):
    """Send an authenticated user to the correct signup wizard step or dashboard."""
    profile = get_profile(user)
    if not profile.is_requester:
        return redirect('dashboard')
    if not profile.is_profile_complete():
        return redirect('signup_profile')
    if profile.requires_wise_banking and not profile.is_wise_recipient_ready():
        return redirect('signup_banking')
    return redirect('signup_complete')


def signup_account(request):
    """Sign-up wizard step 1: create a requestor account, switched on by an emailed link."""
    if request.user.is_authenticated:
        return _signup_redirect_for_user(request.user)

    if request.method == 'POST':
        form = SignupAccountForm(request.POST)
        if form.is_valid():
            user = form.save()
            user.is_active = False
            user.save(update_fields=['is_active'])
            send_activation_email(request, user)
            return render(request, 'tsp/signup/check_email.html', {'email': user.email, 'wizard_step': 1})
    else:
        form = SignupAccountForm()

    return render(request, 'tsp/signup/account.html', {
        'form': form,
        'wizard_step': 1,
    })


def signup_activate(request, uidb64, token):
    """
    Switch on the account an emailed link names.

    Opening the link proves the address, not the password, so the owner still
    signs in. Someone who signed up with another person's address would
    otherwise hand them a signed-in session on an account they still control.
    Only accounts nobody has ever signed in to are switched on, so one an
    admin turned off stays off.
    """
    try:
        user = User.objects.get(pk=urlsafe_base64_decode(uidb64).decode())
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None
    if user is None or user.last_login or not activation_token.check_token(user, token):
        messages.error(request, 'That link has expired or was already used. Sign in, or ask for a new link.')
        return redirect('login')
    user.is_active = True
    user.save(update_fields=['is_active'])
    messages.success(request, 'Email confirmed. Sign in to carry on setting up your account.')
    return redirect('login')


@require_POST
def signup_resend(request):
    """
    Send the confirmation link again.

    The page says the same whether or not the address has an account, so it
    cannot be used to find out who has signed up.
    """
    email = request.POST.get('email', '').strip()
    user = User.objects.filter(email__iexact=email, is_active=False, last_login__isnull=True).first()
    # A few a day is plenty for a lost email, and stops the page being used
    # to flood someone's inbox.
    if user and _under_limit(f'activation-resend:{user.pk}', RESEND_LIMIT, RESEND_WINDOW_SECONDS):
        send_activation_email(request, user)

    return render(request, 'tsp/signup/check_email.html', {'email': email, 'wizard_step': 1})


@login_required
@group_required(GROUP_REQUESTER)
def signup_profile(request):
    """Sign-up wizard step 2: collect required personal and address details."""
    profile = get_profile(request.user)
    # Skip ahead only when nothing is missing here either. An address that a
    # later rule made incomplete, such as a postcode, still has to be filled in,
    # or the dashboard and this step would send the user back and forth.
    if profile.is_profile_complete() and profile.is_wise_recipient_ready():
        return redirect('signup_complete')

    if request.method == 'POST':
        form = OnboardingPersonalForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            form.save(request.user)
            ask_to_confirm_email(request, form)
            profile = get_profile(request.user)
            _mark_profile_completed(profile)
            action = request.POST.get('action', 'continue')
            if action == 'skip':
                if not profile.can_skip_banking:
                    raise PermissionDenied
                messages.info(
                    request,
                    'Skipped Wise banking. You can set it up later from your profile.',
                )
                return redirect('dashboard')
            messages.success(request, 'Profile saved. Set up your Wise banking next.')
            return redirect('signup_banking')
    else:
        form = OnboardingPersonalForm(instance=profile, user=request.user)

    return render(request, 'tsp/signup/profile.html', {
        'form': form,
        'wizard_step': 2,
        'can_skip_banking': profile.can_skip_banking,
    })


@login_required
@group_required(GROUP_REQUESTER)
def signup_banking(request):
    """Sign-up wizard step 3: collect Wise banking details (session-only, not stored)."""
    profile = get_profile(request.user)
    recreate = request.GET.get('recreate') == '1' or request.POST.get('recreate') == '1'
    return_to = request.GET.get('return_to') or request.POST.get('return_to')

    if _is_safe_redirect(request, return_to):
        request.session[RETURN_TO_SESSION_KEY] = return_to
    if request.method == 'GET':
        # Opening the form again means starting over, so bank details from an
        # abandoned attempt go now.
        request.session.pop(SESSION_KEY, None)

    if not profile.is_profile_complete():
        return redirect('signup_profile')

    if profile.is_wise_recipient_ready() and not recreate:
        return redirect('signup_complete')

    if request.method == 'POST':
        form = WiseRecipientForm(request.POST, profile=profile)
        if _counts_as_bank_attempt(form) and not _under_limit(
                f'bank-setup:{request.user.pk}', BANK_SETUP_LIMIT, BANK_SETUP_WINDOW_SECONDS):
            form.add_error(None, BANK_SETUP_REFUSED)
        if form.is_valid():
            state = init_verification_state(banking=banking_session_payload(form.cleaned_data))
            state['started_at'] = time.time()
            request.session[SESSION_KEY] = state
            return redirect('signup_banking_progress')
    else:
        form = WiseRecipientForm(profile=profile)

    return render(request, 'tsp/signup/banking.html', {
        'form': form,
        'wizard_step': 3,
        'wise_api_sandbox': getattr(settings, 'WISE_API_SANDBOX', True),
        'recreate': recreate,
        'can_skip_banking': profile.can_skip_banking,
    })


@login_required
@group_required(GROUP_REQUESTER)
def signup_banking_options(request):
    """Bank and branch lists for the banking step, fetched from Wise on demand."""
    method = request.GET.get('method', '')
    bank_code = request.GET.get('bank_code', '')
    if bank_code:
        # Only a bank from Wise's own list, so made-up codes cannot each cost a call to Wise.
        known = {key for key, _ in bank_choices(method)}
        choices = branch_choices(method, bank_code) if bank_code in known else []

    else:
        choices = bank_choices(method)
    return JsonResponse({'options': [{'value': key, 'label': name} for key, name in choices]})


@login_required
@group_required(GROUP_REQUESTER)
@require_POST
def signup_skip_banking(request):
    """Allow staff/superuser requestors to bypass Wise onboarding."""
    profile = get_profile(request.user)
    if not profile.can_skip_banking:
        raise PermissionDenied
    if not profile.is_profile_complete():
        messages.error(request, 'Complete your profile before continuing.')
        return redirect('signup_profile')
    _mark_profile_completed(profile)
    messages.info(request, 'Skipped Wise banking. You can set it up later from your profile.')
    return redirect('dashboard')


@login_required
@group_required(GROUP_REQUESTER)
def signup_banking_progress(request):
    """Render the step-by-step Wise verification progress UI."""
    profile = get_profile(request.user)

    if not profile.is_profile_complete():
        return redirect('signup_profile')

    if profile.is_wise_recipient_ready():
        return redirect('signup_complete')

    if verification_state(request.session) is None:
        return redirect('signup_banking')

    return render(request, 'tsp/signup/banking_progress.html', {
        'wizard_step': 3,
        'steps': VERIFICATION_STEPS,
        'wise_api_sandbox': getattr(settings, 'WISE_API_SANDBOX', True),
    })


@login_required
@group_required(GROUP_REQUESTER)
def signup_banking_step(request):
    """JSON endpoint: run one Wise verification step in order via AJAX."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    profile = get_profile(request.user)
    state = verification_state(request.session)

    if not state:
        return JsonResponse({'error': 'Verification not started. Go back to banking step.'}, status=400)

    step_id = request.POST.get('step')
    if not step_id:
        return JsonResponse({'error': 'Missing step'}, status=400)

    steps_done = state.get('steps_done', [])
    expected_idx = len(steps_done)
    expected_step = VERIFICATION_STEPS[expected_idx][0] if expected_idx < len(VERIFICATION_STEPS) else None

    if step_id != expected_step:
        return JsonResponse({
            'error': f'Expected step {expected_step}, got {step_id}',
        }, status=400)

    try:
        result = run_verification_step(profile, state, step_id)
    except WiseVerificationError as exc:
        # The page cannot carry on after a failed step, so the bank details go
        # now and the form starts over.
        request.session.pop(SESSION_KEY, None)
        return JsonResponse({
            'step': step_id,
            'status': 'error',
            'message': str(exc),
        }, status=422)

    request.session[SESSION_KEY] = state
    request.session.modified = True

    is_complete = len(state.get('steps_done', [])) >= len(VERIFICATION_STEPS)
    response = {
        'step': step_id,
        'label': dict(VERIFICATION_STEPS)[step_id],
        'status': 'success',
        'result': result,
        'complete': is_complete,
    }
    if is_complete:
        del request.session[SESSION_KEY]
        messages.success(request, 'Wise recipient verified successfully.')
        return_to = request.session.pop(RETURN_TO_SESSION_KEY, None)
        response['redirect'] = return_to or reverse('signup_complete')
    return JsonResponse(response)


@login_required
@group_required(GROUP_REQUESTER)
def signup_complete(request):
    """Sign-up wizard step 4: confirmation when profile and banking are ready."""
    profile = get_profile(request.user)
    if not profile.is_profile_complete():
        return redirect('signup_profile')
    if profile.requires_wise_banking and not profile.is_wise_recipient_ready():
        return redirect('signup_banking')

    return render(request, 'tsp/signup/complete.html', {'wizard_step': 4})


def _paginate(request, queryset, per_page, param='page'):
    """One page of queryset, read from ?<param>= in the query string."""
    return Paginator(queryset, per_page).get_page(request.GET.get(param))


def _requester_sections(request):
    """Dashboard data for someone who submits requests."""
    my_requests = TravelRequest.objects.filter(user=request.user).select_related('event')
    return {
        'my_requests': _paginate(request, my_requests, 20),
        'action_needed': my_requests.filter(state=RequestState.APPROVED),
    }


def _committee_sections(request):
    """Dashboard data for the TSP committee."""
    pending = TravelRequest.objects.filter(
        state=RequestState.SUBMITTED,
    ).select_related('user', 'event').prefetch_related('expenses').order_by('created_at')

    event_summaries = _event_money()
    proposed = Event.objects.filter(
        proposed_by__isnull=False, end_date__gte=timezone.now().date(),
    ).select_related('proposed_by').order_by('start_date')

    approved_total = Expense.objects.filter(
        request__state__in=[
            RequestState.APPROVED, RequestState.ACCEPTED,
            RequestState.SUBMITTED_FOR_PAYMENT, RequestState.PAID,
        ],
        approved_amount__isnull=False,
    ).values('approved_currency').annotate(total=Sum('approved_amount')).order_by('-total')
    # One figure per currency. Adding them together would show 9,000 INR
    # and 90 EUR as 9,090 of something.
    approved_total = [(row['approved_currency'], row['total']) for row in approved_total]

    return {
        'pending': _paginate(request, pending, 20, 'pending_page'),
        'state_counts': (
            TravelRequest.objects.values('state')
            .annotate(count=Count('id'))
            .order_by('state')
        ),
        'approved_total': approved_total,
        'event_summaries': event_summaries,
        'proposed_events': proposed,
    }


_MONEY_STAGES = {
    # stage: (states, which amount counts)
    'draft': ({RequestState.DRAFT}, 'estimated'),
    'requested': ({RequestState.SUBMITTED}, 'estimated'),
    'approved': ({RequestState.APPROVED, RequestState.ACCEPTED, RequestState.SUBMITTED_FOR_PAYMENT}, 'approved'),
}


def _event_money():
    """
    For each event from the last 90 days on: how many requests and how much
    money sit in draft, waiting for review, approved and paid, per currency.
    """
    since = timezone.now().date() - timezone.timedelta(days=90)
    rows = []
    for event in Event.objects.filter(end_date__gte=since).order_by('start_date').prefetch_related(
        'requests__expenses', 'requests__payments',
    ):
        stages = {name: {'count': 0, 'money': {}} for name in (*_MONEY_STAGES, 'paid')}
        for travel_request in event.requests.all():
            if travel_request.state == RequestState.PAID:
                stage = stages['paid']
                paid = travel_request.settled_payment
                amounts = [(paid.currency, paid.amount)] if paid else []
            else:
                name = next((n for n, (states, _) in _MONEY_STAGES.items() if travel_request.state in states), None)
                if name is None:
                    continue
                stage = stages[name]
                amounts = travel_request.totals_by_currency(_MONEY_STAGES[name][1])
            stage['count'] += 1
            for currency, amount in amounts:
                stage['money'][currency] = stage['money'].get(currency, 0) + amount
        for stage in stages.values():
            stage['money'] = sorted(stage['money'].items())
        rows.append({
            'event': event, 'remaining': event.budget_remaining(),
            'stages': [stages[name] for name in ('draft', 'requested', 'approved', 'paid')],
        })
    return rows


def _wise_balance():
    """
    The Wise account's balances as (currency, amount), source currency first,
    or None when Wise cannot be reached. Kept for a minute, so a busy
    dashboard does not ask Wise on every load.
    """
    if not settings.WISE_BALANCE_FETCH:
        return None
    from django.core.cache import caches
    cached = caches['default'].get('wise-balance')
    if cached is not None:
        return cached
    from payments.gateways.wise_api import WiseApiError, get_wise_client
    try:
        balances = get_wise_client().list_balances()
    except (WiseApiError, requests.RequestException):
        return None
    source = settings.WISE_SOURCE_CURRENCY
    result = sorted(
        ((b['amount']['currency'], Decimal(str(b['amount']['value']))) for b in balances if b['amount']['value']),
        key=lambda row: (row[0] != source, row[0]),
    )
    caches['default'].set('wise-balance', result, 60)
    return result


def _shortfalls(event_money, balance):
    """
    Where the Wise balance cannot cover what upcoming events still need, as
    (currency, needed, held, short). An event with a budget needs what is left of it
    to pay; one without needs what is approved or waiting for review.
    """
    if balance is None:
        return []
    today = timezone.now().date()
    needed = {}
    for row in event_money:
        event = row['event']
        if event.end_date < today:
            continue
        draft, requested, approved, paid = row['stages']
        if event.budget is not None:
            spent = dict(paid['money']).get(event.budget_currency, 0)
            needed[event.budget_currency] = needed.get(event.budget_currency, 0) + max(event.budget - spent, 0)
        else:
            for currency, amount in requested['money'] + approved['money']:
                needed[currency] = needed.get(currency, 0) + amount
    held = dict(balance)
    return [
        (currency, amount, held.get(currency, Decimal('0')), amount - held.get(currency, Decimal('0')))
        for currency, amount in sorted(needed.items())
        if amount > held.get(currency, Decimal('0'))
    ]


def _finance_sections(request):
    """Dashboard data for the finance team."""
    recently_paid = TravelRequest.objects.filter(
        state=RequestState.PAID,
    ).select_related('user', 'event').prefetch_related('payments', 'expenses').order_by('-updated_at')

    travel_request_ct = ContentType.objects.get_for_model(TravelRequest)
    # Only transfers that went through count as paid, one figure per currency.
    paid_total = [
        (row['currency'], row['total'])
        for row in PaymentRecord.objects.filter(
            content_type=travel_request_ct, status__in=SETTLED_PAYMENT_STATUSES,
        ).values('currency').annotate(total=Sum('amount')).order_by('-total')
    ]

    with_advance = TravelRequest.objects.filter(
        payments__purpose='advance', refund_settled_at__isnull=True,
    ).distinct().select_related('user', 'event').prefetch_related('payments', 'expenses')

    return {
        'payment_queue': TravelRequest.objects.filter(
            state=RequestState.SUBMITTED_FOR_PAYMENT,
        ).select_related('user', 'event').prefetch_related('expenses', 'payments')
        .annotate(doc_count=Count('documents')),
        'advance_queue': TravelRequestPayableAdapter().get_queue().filter(state=RequestState.ACCEPTED),
        'owed_back': [r for r in with_advance if r.amount_owed_back()],
        'recently_paid': _paginate(request, recently_paid, 10, 'paid_page'),
        'paid_total': paid_total,
        'event_summaries': (event_money := _event_money()),
        'wise_balance': (balance := _wise_balance()),
        'shortfalls': _shortfalls(event_money, balance),
        'needs_budget': Event.objects.filter(
            budget__isnull=True, end_date__gte=timezone.now().date(),
        ).order_by('start_date'),
        'wise_sandbox': settings.WISE_API_SANDBOX,
        'wise_balance_on': settings.WISE_BALANCE_FETCH,
    }


@login_required
def dashboard(request):
    """Home page, with a section for each group the user belongs to."""
    profile = get_profile(request.user)
    if not (profile.is_requester or profile.is_tsp or profile.is_finance):
        raise PermissionDenied

    context = {'profile': profile}
    if profile.is_requester:
        context.update(_requester_sections(request))
    if profile.is_tsp:
        context.update(_committee_sections(request))
    else:
        context['organised'] = [
            {
                'event': event,
                'pending': event.requests.filter(state=RequestState.SUBMITTED)
                                .select_related('user').prefetch_related('expenses').order_by('created_at'),
                'approved': event.approved_against_budget(),
                'remaining': event.budget_remaining(),
            }
            for event in Event.organised_by(request.user).order_by('start_date')
        ]
    if profile.is_finance:
        context.update(_finance_sections(request))

    return render(request, 'tsp/dashboard/index.html', context)


@login_required
@group_required(GROUP_REQUESTER)
def onboarding_personal(request):
    """Legacy URL alias for the profile onboarding step."""
    return redirect('signup_profile')


@login_required
@group_required(GROUP_REQUESTER)
def onboarding_wise(request):
    """Legacy URL alias for the Wise banking onboarding step."""
    return redirect('signup_banking')


def _date_param(request, name):
    """A YYYY-MM-DD date from the query string, or None if it is missing or not a date."""
    try:
        return parse_date(request.GET.get(name) or '')
    except ValueError:
        return None


@login_required
def event_list(request):
    """List TSP events, optionally filtered by start/end date query params."""
    profile = get_profile(request.user)
    date_from = _date_param(request, 'from')
    date_to = _date_param(request, 'to')

    events = Event.objects.annotate(request_count=Count('requests'))
    if date_from:
        events = events.filter(start_date__gte=date_from)
    if date_to:
        events = events.filter(end_date__lte=date_to)

    # What people can still act on comes first, soonest first; past events
    # follow, most recent first, since that is the one someone is looking for.
    events = list(events)
    upcoming = sorted((e for e in events if not e.is_past), key=lambda e: e.start_date)
    past = sorted((e for e in events if e.is_past), key=lambda e: e.start_date, reverse=True)

    return render(request, 'tsp/events/list.html', {
        'upcoming': upcoming,
        'past': past,
        'profile': profile,
        'date_from': date_from or '',
        'date_to': date_to or '',
    })


@login_required
def event_create(request):
    """
    Create an event. The committee sets everything; anyone else proposes one,
    which stays closed to requests until the committee sets its budget and opens it.
    """
    profile = get_profile(request.user)
    if profile.is_tsp:
        form_class = partial(EventForm, sets_budget=profile.is_finance)
    else:
        form_class = EventProposalForm
    if request.method == 'POST':
        form = form_class(request.POST)
        if form.is_valid():
            event = form.save(commit=False)
            if not profile.is_tsp:
                event.accepting_requests = False
                event.proposed_by = request.user
            event.save()
            form.save_m2m()
            audit('event.created' if profile.is_tsp else 'event.proposed', user=request.user,
                  target=event.name,
                  detail=f'{event.start_date} to {event.end_date}'
                         + (f', budget {event.budget} {event.budget_currency}'
                            if event.budget else '')
                         + (f', suggested {event.suggested_budget} {event.budget_currency}'
                            if event.suggested_budget else ''))
            if profile.is_tsp:
                messages.success(request, 'Event created successfully.')
            else:
                from profile.services.push_notifications import send_push_to_group
                send_push_to_group(
                    GROUP_TSP, head='Event proposed',
                    body=f'{request.user.get_full_name() or request.user.username} proposed {event.name}.',
                    url=reverse('event_edit', args=[event.pk]),
                )
                messages.success(
                    request,
                    'Thanks. The committee will set a budget and open it for requests.',
                )
            return redirect('event_list')
    else:
        form = form_class()

    return render(request, 'tsp/events/form.html', {'form': form, 'profile': profile})


@login_required
def event_edit(request, pk):
    """
    Edit an event. The committee edits everything but the budget, which is
    finance's to set; someone in both groups edits both.
    """
    event = get_object_or_404(Event, pk=pk)
    profile = get_profile(request.user)
    if profile.is_tsp:
        form_class = partial(EventForm, sets_budget=profile.is_finance)
        template = 'tsp/events/form.html'
    elif profile.is_finance:
        form_class = EventBudgetForm
        template = 'tsp/events/budget.html'
    else:
        raise PermissionDenied
    if request.method == 'POST':
        form = form_class(request.POST, instance=event)
        if form.is_valid():
            event = form.save(commit=False)
            if form_class is not EventBudgetForm:
                # The committee has reviewed it, so it leaves the proposals queue.
                event.proposed_by = None
            event.save()
            form.save_m2m()
            audit('event.updated', user=request.user, target=event.name,
                  detail=', '.join(form.changed_data) or 'no fields changed')
            messages.success(request, 'Budget saved.' if form_class is EventBudgetForm else 'Event updated.')
            return redirect('dashboard' if form_class is EventBudgetForm else 'event_list')
    else:
        form = form_class(instance=event)

    return render(request, template, {'form': form, 'profile': profile, 'event': event})


@login_required
@group_required(GROUP_TSP)
@require_POST
def event_delete(request, pk):
    """Delete an event, as long as nobody has applied to it yet."""
    event = get_object_or_404(Event, pk=pk)
    # The database refuses to delete an event with requests, even one sent a
    # moment ago, so only an empty one can go. Closing it stops new requests instead.
    name = event.name
    try:
        event.delete()
    except ProtectedError:
        messages.error(
            request,
            f'{name} already has travel requests, so it cannot be deleted. '
            'Close it to stop new requests instead.',
        )
        return redirect('event_list')

    audit('event.deleted', user=request.user, target=name,
          detail=f'{event.start_date} to {event.end_date}')
    messages.success(request, f'{name} deleted.')
    return redirect('event_list')


@login_required
@group_required(GROUP_REQUESTER, GROUP_TSP)
def check_wise_account(request):
    """Live Wise recipient status; recreates via Wise if the account is missing."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    profile = get_profile(request.user)
    banking = None
    auto_recreate = request.POST.get('recreate') == '1'

    if auto_recreate:
        form = WiseRecipientForm(request.POST, profile=profile)
        if _counts_as_bank_attempt(form) and not _under_limit(
                f'bank-setup:{request.user.pk}', BANK_SETUP_LIMIT, BANK_SETUP_WINDOW_SECONDS):
            return JsonResponse({'ready': False, 'message': BANK_SETUP_REFUSED}, status=429)
        if not form.is_valid():
            return JsonResponse({
                'ready': False,
                'needs_recreate': True,
                'message': (form.errors['current_password'][0] if 'current_password' in form.errors
                            else 'Invalid bank details.'),
                'errors': form.errors,
            }, status=422)
        banking = banking_session_payload(form.cleaned_data)

    result = check_wise_recipient(profile, banking=banking, auto_recreate=auto_recreate)

    if result.get('needs_recreate') and not banking:
        result['redirect'] = (
            reverse('signup_banking')
            + '?recreate=1&return_to='
            + reverse('request_create')
        )
        result['auto_redirect'] = True

    return JsonResponse(result)


def _tell_about_refund(request, owing):
    currency, amount = owing.amount_owed_back()
    messages.error(
        request,
        f'{owing.reference} has {amount} {currency} of its advance to pay back. '
        'New requests can be submitted once finance has confirmed it.',
    )


def _payout(profile):
    """The requester's payout currency and today's average rates into it, for the form's estimate."""
    if not (profile.is_requester and profile.wise_account_id):
        return None
    exchange_rates.ensure_recent()
    return {'currency': profile.banking_currency, 'rates': exchange_rates.averages_into(profile.banking_currency)}


@login_required
@group_required(GROUP_REQUESTER, GROUP_TSP)
def request_create(request):
    """Create a new travel request as draft with expense line items."""
    profile = get_profile(request.user)
    onboarding = profile.onboarding_step()
    if onboarding:
        return redirect(onboarding)
    owing = request_owing_money(request.user)
    if owing:
        _tell_about_refund(request, owing)
        return redirect('request_detail', pk=owing.pk)
    if request.method == 'POST':
        form = TravelRequestForm(request.POST)
        form_is_valid = form.is_valid()
        formset = ExpenseFormSet(request.POST, form_kwargs={'event': form.cleaned_data.get('event')})
        if form_is_valid and formset.is_valid():
            with transaction.atomic():
                travel_request = form.save(commit=False)
                travel_request.user = request.user
                travel_request.state = RequestState.DRAFT
                travel_request.save()
                formset.instance = travel_request
                formset.save()
                create_request_with_history(travel_request, request.user)
            messages.success(request, 'Request created as draft.')
            return redirect('request_detail', pk=travel_request.pk)
    else:
        form = TravelRequestForm()
        formset = ExpenseFormSet()

    return render(request, 'tsp/requests/form.html', {
        'form': form,
        'formset': formset,
        'event_categories': _categories_by_event(form),
        'profile': profile,
        'is_edit': False,
        'payout': _payout(profile),
        'show_account_check': profile.is_requester,
        'banking_form': WiseRecipientForm(profile=profile) if profile.is_requester else None,
    })


def _reviews(user, profile, event):
    """True when ``user`` may approve and reject requests for ``event``."""
    return profile.is_tsp or event.is_organised_by(user)


def _sees_internal_notes(user, profile, travel_request):
    """Committee, finance and the event's organisers see internal notes, except on their own request."""
    if travel_request.user_id == user.id:
        return False
    return profile.is_committee or travel_request.event.is_organised_by(user)


_OVERAGE_NOTE = re.compile(r'\s*\[over budget by [^\]]*\]')
_WISE_NOTE = re.compile(r'Payment via Wise (Batch CSV|API Transfer)')


def _history_for(travel_request, sees_internal):
    """
    The request's history. A requester sees which team acted rather than
    who, and not the committee's internal budget note.
    """
    history = list(travel_request.state_history.select_related('user'))
    if sees_internal:
        return history
    for entry in history:
        notes = _OVERAGE_NOTE.sub('', entry.notes or '')
        entry.notes = _WISE_NOTE.sub('Paid via Wise', notes).strip()
        if entry.user_id != travel_request.user_id:
            actor = get_profile(entry.user)
            entry.actor = 'Finance' if actor.is_finance and not actor.is_tsp else 'TSP Committee'
    return history


def _review_lines(travel_request):
    """
    Each expense with the currencies it can be approved in, already converted.

    A line requested in a payout currency starts in that currency. Anything
    else starts converted into the first payout currency at the 90-day average,
    unless no rate is known, when it stays as requested rather than turning
    9000 INR into 9000 EUR.
    """
    exchange_rates.ensure_recent()
    end = exchange_rates.window_end_for(travel_request.event)
    payout = list(settings.PAYOUT_CURRENCIES)
    lines = []
    for expense in travel_request.expenses.all():
        source = expense.estimated_currency
        share = travel_request.event.share_of(expense.estimated_amount)
        options = []
        for code in dict.fromkeys(payout + [source]):
            option = {'code': code, 'amount': share, 'full': expense.estimated_amount, 'rates': None}
            if code != source:
                # The same way round as the rate locked at approval, and the
                # readable way too: 1 USD = 95.27 INR.
                rates = exchange_rates.summary(code, source, end)
                if rates is None:
                    continue
                option['amount'] = (share / rates.average).quantize(Decimal('0.01'))
                option['full'] = (expense.estimated_amount / rates.average).quantize(Decimal('0.01'))
                option['rates'] = rates
            options.append(option)
        default = source if source in payout or len(options) == 1 else options[0]['code']
        for option in options:
            option['selected'] = option['code'] == default
        lines.append({'expense': expense, 'options': options})
    return lines


@login_required
@staff_or_owner
def request_detail(request, pk, travel_request, profile):
    """Display request details, comments, documents, and state history."""
    sees_internal = _sees_internal_notes(request.user, profile, travel_request)
    comments = travel_request.comments.select_related('user')
    if not sees_internal:
        comments = comments.filter(internal=False)

    comment_form = CommentForm(can_write_internal=sees_internal)
    doc_form = DocumentUploadForm()
    review_lines = None
    can_review = _reviews(request.user, profile, travel_request.event)
    if can_review and travel_request.state == RequestState.SUBMITTED:
        review_lines = _review_lines(travel_request)
    return render(request, 'tsp/requests/detail.html', {
        'review_lines': review_lines,
        'can_review': can_review,
        'payout_currencies': settings.PAYOUT_CURRENCIES,
        'travel_request': travel_request,
        'profile': profile,
        'comments': comments,
        'sees_internal_notes': sees_internal,
        'documents': travel_request.documents.select_related('uploaded_by'),
        'state_history': _history_for(travel_request, sees_internal),
        'comment_form': comment_form,
        'doc_form': doc_form,
        'missing_docs': travel_request.missing_document_kinds(),
        'has_actuals': travel_request.has_all_actual_amounts(),
    })


def _categories_by_event(form):
    """Each selectable event's expense categories, for the form to switch between."""
    return {str(event.pk): event.expense_categories() for event in form.fields['event'].queryset}


def _send_back_for_review(travel_request, user):
    """New amounts mean the old approval, and any advance it allowed, no longer apply."""
    travel_request.expenses.update(
        approved_amount=None, approved_currency=None,
        approval_rate=None, approval_rate_date=None,
    )
    travel_request.approved_at = None
    travel_request.approval_percent = None
    travel_request.advance_amount = None
    travel_request.advance_currency = None
    travel_request.advance_requested_at = None
    transition_request(
        travel_request, RequestState.SUBMITTED, user,
        notes='Amounts changed after approval, so it needs approving again.',
    )


@login_required
def request_edit(request, pk):
    """Edit a draft or submitted request owned by the current user."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    profile = get_profile(request.user)

    if travel_request.user_id != request.user.id:
        raise PermissionDenied
    needs_review_again = travel_request.can_change_amounts()
    if not (travel_request.is_editable_by_requester or needs_review_again):
        messages.error(request, 'This request can no longer be edited.')
        return redirect('request_detail', pk=pk)

    if request.method == 'POST':
        # Locked and checked again, so an approval made while the form was open
        # is not saved over, and an edit that fails part way saves nothing.
        with transaction.atomic():
            travel_request = TravelRequest.objects.select_for_update().get(pk=pk)
            needs_review_again = travel_request.can_change_amounts()
            if not (travel_request.is_editable_by_requester or needs_review_again):
                messages.error(request, 'This request can no longer be edited.')
                return redirect('request_detail', pk=pk)
            form = TravelRequestForm(request.POST, instance=travel_request)
            form_is_valid = form.is_valid()
            event = form.cleaned_data.get('event') or travel_request.event
            formset = ExpenseFormSet(request.POST, instance=travel_request, form_kwargs={'event': event})
            if form_is_valid and formset.is_valid():
                form.save()
                formset.save()
                if needs_review_again:
                    _send_back_for_review(travel_request, request.user)
                    messages.success(request, 'Request updated and sent back to the committee for approval.')
                else:
                    messages.success(request, 'Request updated.')
                return redirect('request_detail', pk=pk)
    else:
        form = TravelRequestForm(instance=travel_request)
        formset = ExpenseFormSet(instance=travel_request)

    return render(request, 'tsp/requests/form.html', {
        'form': form,
        'formset': formset,
        'event_categories': _categories_by_event(form),
        'profile': profile,
        'travel_request': travel_request,
        'is_edit': True,
        'payout': _payout(get_profile(travel_request.user)),
        'needs_review_again': needs_review_again,
    })


@login_required
@require_POST
def request_submit(request, pk):
    """Submit a draft request for TSP committee review."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    if travel_request.user_id != request.user.id:
        raise PermissionDenied

    onboarding = get_profile(request.user).onboarding_step()
    if onboarding:
        messages.error(request, 'Finish setting up your payout account before submitting.')
        return redirect(onboarding)
    owing = request_owing_money(request.user)
    if owing:
        _tell_about_refund(request, owing)
        return redirect('request_detail', pk=pk)

    try:
        transition_request(travel_request, RequestState.SUBMITTED, request.user)
        messages.success(request, 'Request submitted for review.')
    except StateTransitionError as e:
        messages.error(request, str(e))
    return redirect('request_detail', pk=pk)


def _set_advance(travel_request, data):
    """Record the advance the committee allows, if any. Returns an error or None."""
    travel_request.advance_amount = None
    travel_request.advance_currency = None
    raw = (data.get('advance_amount') or '').strip()
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return 'The advance amount is not a number.'
    currency = data.get('advance_currency', '')
    approved = dict(travel_request.totals_approved())
    if amount <= 0 or amount > approved.get(currency, Decimal('0')):
        return (
            f'An advance has to be more than zero and no more than what is approved '
            f'in {currency} ({approved.get(currency, Decimal("0"))} {currency}).'
        )
    travel_request.advance_amount = amount
    travel_request.advance_currency = currency
    return None


@login_required
@require_POST
def request_approve(request, pk):
    """Approve a submitted request and set per-expense approved amounts."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    profile = get_profile(request.user)
    if not _reviews(request.user, profile, travel_request.event):
        raise PermissionDenied
    if travel_request.user_id == request.user.id:
        messages.error(request, 'You cannot review your own request. Another committee member has to.')
        return redirect('request_detail', pk=pk)
    form = ApprovalForm(request.POST)
    if not form.is_valid():
        errors = form.errors.get('notes') or form.errors.get('approval_percent') or ['Check the approval notes.']
        messages.error(request, ' '.join(errors))
        return redirect('request_detail', pk=pk)
    notes = form.cleaned_data.get('notes', '')
    travel_request.approval_percent = form.cleaned_data.get('approval_percent')

    error = None
    # Everything below is undone if any check refuses the approval, so a
    # refused approval leaves no amounts behind to skew the event budget.
    with transaction.atomic():
        _lock_budget(travel_request.event)
        error = _apply_approved_amounts(travel_request, request.POST)
        # An advance is a decision to pay money early, so it stays with the committee.
        if not error and profile.is_tsp:
            error = _set_advance(travel_request, request.POST)
        if not error:
            notes, error = _check_budget(travel_request, request.POST, notes, profile.is_tsp)
        if not error:
            try:
                transition_request(travel_request, RequestState.APPROVED, request.user, notes)
            except StateTransitionError as exc:
                error = str(exc)
        if error:
            transaction.set_rollback(True)

    if error:
        messages.error(request, error)
    else:
        messages.success(request, 'Request approved.')
    return redirect('request_detail', pk=pk)


def _apply_approved_amounts(travel_request, data):
    """Save the approved amount and currency for each line. Returns an error or None."""
    allowed = set(settings.PAYOUT_CURRENCIES)
    expenses = {str(e.pk): e for e in travel_request.expenses.all()}
    rate_end = exchange_rates.window_end_for(travel_request.event)
    for eid in data.getlist('expense_id'):
        expense = expenses.get(eid)
        if expense is None:
            continue
        raw = data.get(f'approved_amount_{eid}', '').strip()
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return f'Enter an approved amount for {expense.get_category_display()}, as a number such as 73.62.'
        if not amount.is_finite() or amount < 0:
            return 'Approved amounts have to be zero or more.'
        currency = data.get(f'approved_currency_{eid}', '')
        if currency not in allowed | {expense.estimated_currency}:
            return 'Approve each line in one of the currencies offered for it.'
        expense.approved_amount = amount
        expense.approved_currency = currency
        if currency != expense.estimated_currency:
            exchange_rates.ensure_recent()
        exchange_rates.lock_approval_rate(expense, rate_end)
        expense.save(update_fields=[
            'approved_amount', 'approved_currency', 'approval_rate', 'approval_rate_date',
        ])
    return None


def _lock_budget(event):
    """
    Hold the event's row until the approval commits.

    Two approvals for one event at the same moment would each see the budget
    before the other's amounts and both pass. The second now waits, then
    checks against what the first approved.
    """
    Event.objects.select_for_update().get(pk=event.pk)


def _check_budget(travel_request, data, notes, may_overspend=True):
    """
    Refuse an approval that takes the event past its budget, unless overridden.

    The budget is the committee's own limit, so going past it is allowed but
    has to be deliberate, and the overage is written into the history. An
    organiser works within the budget they were given, so only the committee
    can go past it. Returns the notes to record and an error or None.
    """
    event = travel_request.event
    remaining = event.budget_remaining()
    if remaining is None or remaining >= 0:
        return notes, None
    if not may_overspend:
        return notes, (
            f'Approving this would put {event.name} {abs(remaining)} {event.budget_currency} '
            'over its budget. Lower the amounts, or ask the committee to approve it.'
        )
    if data.get('over_budget') != '1':
        return notes, (
            f'Approving this would put {event.name} {abs(remaining)} '
            f'{event.budget_currency} over its budget. Approve again with the '
            f'over-budget box ticked to go ahead anyway.'
        )
    overage = f'[over budget by {abs(remaining)} {event.budget_currency}]'
    return (f'{notes} {overage}' if notes else overage), None


@login_required
@require_POST
def request_batch_review(request):
    """Approve or reject several submitted requests in one go."""
    action = request.POST.get('action')
    ids = request.POST.getlist('request_ids')
    # The same rules as a single review, since these notes go into the history too.
    notes_form = RejectForm({'notes': request.POST.get('notes', '')})
    if request.POST.get('notes', '').strip() and not notes_form.is_valid():
        messages.error(request, ' '.join(notes_form.errors['notes']))
        return redirect('dashboard')
    notes = notes_form.cleaned_data.get('notes', '') if notes_form.is_valid() else ''

    if action not in ('approve', 'reject'):
        messages.error(request, 'Choose whether to approve or reject.')
        return redirect('dashboard')
    if not ids:
        messages.error(request, 'Select at least one request first.')
        return redirect('dashboard')
    if action == 'reject' and not notes:
        messages.error(request, 'Rejecting needs a note saying why.')
        return redirect('dashboard')

    profile = get_profile(request.user)
    selected = TravelRequest.objects.filter(
        pk__in=ids, state=RequestState.SUBMITTED,
    ).select_related('event').prefetch_related('expenses')
    not_yours = 0
    if not profile.is_tsp:
        organised = Event.organised_by(request.user)
        if not organised.exists():
            raise PermissionDenied
        not_yours = selected.exclude(event__in=organised).count()
        selected = selected.filter(event__in=organised)

    reviewed, own, over_budget = 0, 0, []
    for travel_request in selected:
        if travel_request.user_id == request.user.id:
            own += 1
            continue
        with transaction.atomic():
            if action == 'approve':
                _lock_budget(travel_request.event)
                # Approving in bulk approves the event's usual share of what was
                # asked for. Left unpriced, the request would reach finance worth
                # nothing, since a missing approved amount counts for nothing.
                for expense in travel_request.expenses.all():
                    if expense.approved_amount is None:
                        expense.approved_amount = travel_request.event.share_of(expense.estimated_amount)
                        travel_request.approval_percent = travel_request.event.approval_percent
                        expense.approved_currency = expense.estimated_currency
                        expense.save(update_fields=['approved_amount', 'approved_currency'])
                remaining = travel_request.event.budget_remaining()
                if remaining is not None and remaining < 0:
                    # Going over budget needs a deliberate decision, which a
                    # batch cannot record, so it waits for a single review.
                    transaction.set_rollback(True)
                    over_budget.append(travel_request.reference)
                    continue
                target = RequestState.APPROVED
            else:
                target = RequestState.REJECTED
            try:
                transition_request(travel_request, target, request.user, notes)
                reviewed += 1
            except StateTransitionError:
                transaction.set_rollback(True)

    if reviewed:
        which = (
            'All selected requests' if reviewed == len(ids)
            else f'{reviewed} request{"s" if reviewed != 1 else ""}'
        )
        messages.success(request, f'{which} {action}d.')
    if own:
        messages.warning(
            request,
            f'Left {own} of your own request{"s" if own != 1 else ""} alone. '
            'Another committee member has to review those.',
        )
    if over_budget:
        messages.warning(
            request,
            f'Not approved, as they would go over the event budget: {", ".join(over_budget)}. '
            'Review them one at a time to approve over budget.',
        )
    if not_yours:
        messages.warning(request, f'Left {not_yours} request{"s" if not_yours != 1 else ""} for events you do not organise.')
    ignored = len(ids) - reviewed - own - len(over_budget) - not_yours
    if ignored:
        messages.warning(
            request,
            f'{ignored} selected request{"s were" if ignored != 1 else " was"} '
            'left alone, having already moved on.',
        )
    return redirect('dashboard')


@login_required
@require_POST
def request_reject(request, pk):
    """Reject a submitted request with required notes."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    if not _reviews(request.user, get_profile(request.user), travel_request.event):
        raise PermissionDenied
    if travel_request.user_id == request.user.id:
        messages.error(request, 'You cannot review your own request. Another committee member has to.')
        return redirect('request_detail', pk=pk)
    form = RejectForm(request.POST)
    if form.is_valid():
        try:
            transition_request(
                travel_request, RequestState.REJECTED, request.user, form.cleaned_data['notes'],
            )
            messages.success(request, 'Request rejected.')
        except StateTransitionError as e:
            messages.error(request, str(e))
    else:
        messages.error(request, 'Rejection notes are required.')
    return redirect('request_detail', pk=pk)


@login_required
@require_POST
def request_cancel(request, pk):
    """Cancel a request as its owner, the TSP committee, or finance once it waits for payment."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    profile = get_profile(request.user)
    is_owner = travel_request.user_id == request.user.id
    waits_for_payment = travel_request.state == RequestState.SUBMITTED_FOR_PAYMENT

    if not (is_owner or profile.is_tsp or (profile.is_finance and waits_for_payment)):
        raise PermissionDenied
    # Any money still on its way, the advance included, has to settle or be
    # cancelled in Wise first, or a transfer or batch file is left behind.
    if travel_request.payments.exclude(status__in=['success', 'completed', 'failed', 'cancelled']).exists():
        messages.error(
            request,
            'A payment for this request is already under way. Cancel it in Wise first, '
            'then refresh its status here.',
        )
        return redirect('request_detail', pk=pk)

    try:
        transition_request(travel_request, RequestState.CANCELLED, request.user)
        messages.success(request, 'Request cancelled.')
    except StateTransitionError as e:
        messages.error(request, str(e))
    return redirect('request_detail', pk=pk)


@login_required
def request_accept_page(request, pk):
    """Show acceptance confirmation checkboxes for an approved request."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    profile = get_profile(request.user)

    if travel_request.user_id != request.user.id:
        raise PermissionDenied
    if travel_request.state != RequestState.APPROVED:
        messages.error(request, 'This request is not awaiting acceptance.')
        return redirect('request_detail', pk=pk)

    form = AcceptForm()
    return render(request, 'tsp/requests/accept.html', {
        'travel_request': travel_request,
        'form': form,
        'profile': profile,
    })


@login_required
@require_POST
def request_accept(request, pk):
    """Accept an approved request after all confirmations are checked."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    if travel_request.user_id != request.user.id:
        raise PermissionDenied

    form = AcceptForm(request.POST)
    if form.is_valid():
        try:
            transition_request(travel_request, RequestState.ACCEPTED, request.user)
        except StateTransitionError as e:
            messages.error(request, str(e))
            return redirect('request_detail', pk=pk)
        messages.success(
            request,
            f'You are all set. Enjoy the event, {travel_request.event.name}!',
        )
        return redirect('dashboard')
    messages.error(request, 'All confirmations are required.')
    return redirect('request_accept_page', pk=pk)


@login_required
def request_actuals(request, pk):
    """Let the requestor report what each approved line actually cost."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    if travel_request.user_id != request.user.id:
        raise PermissionDenied
    if travel_request.state != RequestState.ACCEPTED:
        messages.error(request, 'Actual costs can only be reported on an accepted request.')
        return redirect('request_detail', pk=pk)

    queryset = travel_request.expenses.filter(approved_amount__isnull=False)
    if request.method == 'POST':
        formset = ExpenseActualFormSet(request.POST, instance=travel_request, queryset=queryset)
        if formset.is_valid():
            formset.save()
            messages.success(request, 'Actual costs saved.')
            return redirect(reverse('request_detail', args=[pk]) + '#reimbursement')
    else:
        formset = ExpenseActualFormSet(instance=travel_request, queryset=queryset)

    return render(request, 'tsp/requests/actuals.html', {
        'travel_request': travel_request,
        'formset': formset,
    })


@login_required
@require_POST
def request_submit_for_payment(request, pk):
    """Move an accepted request to finance queue when all documents are uploaded."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    if travel_request.user_id != request.user.id:
        raise PermissionDenied

    if not travel_request.has_all_required_documents():
        missing = ', '.join(travel_request.missing_document_kinds())
        messages.error(request, f'Missing required documents: {missing}')
        return redirect('request_detail', pk=pk)

    if travel_request.event.reimbursement_window_closed():
        messages.error(
            request,
            f'Claims for {travel_request.event.name} closed on '
            f'{travel_request.event.reimbursement_deadline()}.',
        )
        return redirect('request_detail', pk=pk)

    # Finance pays the lesser of approved and actual, so the actual costs have
    # to be in before the request reaches them.
    if not travel_request.has_all_actual_amounts():
        messages.error(request, 'Report what each approved expense actually cost first.')
        return redirect('request_actuals', pk=pk)

    # The final amount takes the advance off, so it has to be settled first.
    advance_in_flight = travel_request.payments.filter(purpose='advance').exclude(
        status__in=['success', 'completed', 'failed', 'cancelled'],
    ).exists()
    if advance_in_flight:
        messages.error(request, 'Your advance is still being paid. Submit once it has arrived.')
        return redirect('request_detail', pk=pk)

    try:
        transition_request(travel_request, RequestState.SUBMITTED_FOR_PAYMENT, request.user)
        messages.success(request, 'Request submitted for payment.')
    except StateTransitionError as e:
        messages.error(request, str(e))
    return redirect('request_detail', pk=pk)


@login_required
@require_POST
def request_take_back(request, pk):
    """Pull a request back out of the payment queue so the requester can fix it."""
    # Locked as finance's payment locks it, so a payment starting at the same
    # moment either goes first and is seen here, or waits and finds it gone.
    with transaction.atomic():
        travel_request = get_object_or_404(TravelRequest.objects.select_for_update(), pk=pk)
        if travel_request.user_id != request.user.id:
            raise PermissionDenied
        if not travel_request.can_take_back_from_payment():
            messages.error(request, 'Finance has already started paying this request.')
            return redirect('request_detail', pk=pk)

        transition_request(
            travel_request, RequestState.ACCEPTED, request.user,
            notes='Taken back from the payment queue to edit.',
        )
    messages.success(request, 'Taken back. Fix your costs or documents, then submit for payment again.')
    return redirect('request_detail', pk=pk)


@login_required
@require_POST
def request_advance(request, pk):
    """Ask finance to pay the advance the committee allowed, before the event."""
    from profile.services.push_notifications import send_push_to_group

    travel_request = get_object_or_404(TravelRequest, pk=pk)
    if travel_request.user_id != request.user.id:
        raise PermissionDenied
    if not travel_request.can_request_advance():
        messages.error(request, 'No advance can be requested on this request.')
        return redirect('request_detail', pk=pk)

    travel_request.advance_requested_at = timezone.now()
    travel_request.save(update_fields=['advance_requested_at', 'updated_at'])
    send_push_to_group(
        GROUP_FINANCE,
        head='Advance requested',
        body=(
            f'#{travel_request.pk} {travel_request.event.name}: '
            f'{travel_request.advance_amount} {travel_request.advance_currency} before the event.'
        ),
        url=reverse('request_detail', args=[pk]),
        exclude_user=request.user,
    )
    messages.success(request, 'Advance requested. Finance will pay it before the event.')
    return redirect('request_detail', pk=pk)


@login_required
@group_required(GROUP_FINANCE)
@require_POST
def request_settle(request, pk):
    """Finance closes a request the advance already covered, or records a refund."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    action = request.POST.get('action')
    if travel_request.user_id == request.user.id:
        messages.error(request, 'You cannot settle your own request. Another finance member has to.')
        return redirect('request_detail', pk=pk)


    if action == 'close' and travel_request.advance_covers_costs():
        transition_request(
            travel_request, RequestState.PAID, request.user,
            notes='The advance already covered the costs.',
        )
        messages.success(request, 'Closed: the advance covered everything.')
    elif action == 'refund' and travel_request.amount_owed_back():
        travel_request.refund_settled_at = timezone.now()
        travel_request.save(update_fields=['refund_settled_at', 'updated_at'])
        messages.success(request, 'Refund recorded. The requester can submit new requests again.')
    else:
        messages.error(request, 'There is nothing to settle on this request.')
    return redirect('request_detail', pk=pk)


@login_required
@staff_or_owner
@require_POST
def request_comment(request, pk, travel_request, profile):
    """Add a comment and notify the other party via push notification."""
    form = CommentForm(
        request.POST,
        can_write_internal=_sees_internal_notes(request.user, profile, travel_request),
    )
    if form.is_valid():
        comment = form.save(commit=False)
        comment.request = travel_request
        comment.user = request.user
        comment.save()
        from profile.services.push_notifications import notify_request_comment
        notify_request_comment(travel_request, request.user, internal=comment.internal)
        messages.success(request, 'Comment added.')
    return redirect('request_detail', pk=pk)


@login_required
@staff_or_owner
@require_POST
def request_document_upload(request, pk, travel_request, profile):
    """Upload a receipt, invoice, or signed form to a travel request."""
    # Documents are the requester's evidence for the claim. Letting someone who
    # reviews or pays it add to that evidence would defeat the point of it.
    if travel_request.user_id != request.user.id:
        raise PermissionDenied
    if travel_request.state != RequestState.ACCEPTED:
        messages.error(
            request,
            'Documents can be added after you accept the approval, and until the '
            'request goes to finance. Take it back from finance to add more.',
        )
        return redirect('request_detail', pk=pk)
    # The first row is "kind"/"file"; each added row is "kind_N"/"file_N".
    rows = [('kind', 'file')] + [
        (f'kind_{key[5:]}', key) for key in request.FILES if key.startswith('file_')
    ]
    forms_to_save = []
    for kind_key, file_key in rows:
        if not request.FILES.get(file_key):
            continue
        form = DocumentUploadForm(
            {'kind': request.POST.get(kind_key)}, {'file': request.FILES[file_key]},
        )
        if not form.is_valid():
            errors = form.errors.get('file') or form.errors.get('kind') or ['Check the file.']
            messages.error(request, f'{request.FILES[file_key].name}: {errors[0]} Nothing was uploaded.')
            return redirect(reverse('request_detail', args=[pk]) + '#documents')
        forms_to_save.append((form, request.FILES[file_key]))
    if not forms_to_save:
        messages.error(request, 'Choose a file to upload.')
        return redirect(reverse('request_detail', args=[pk]) + '#documents')

    for form, uploaded in forms_to_save:
        doc = form.save(commit=False)
        doc.request = travel_request
        doc.uploaded_by = request.user
        doc.original_name = uploaded.name
        doc.mimetype = uploaded.content_type or 'application/octet-stream'
        doc.size = uploaded.size
        doc.save()
    count = len(forms_to_save)
    done = '' if travel_request.missing_document_kinds() else ' You can now submit for payment.'
    messages.success(request, f'{count} document{"s" if count != 1 else ""} uploaded.{done}')
    return redirect(reverse('request_detail', args=[pk]) + '#documents')


# Once a request is with finance its documents back a payment, so they stay.
LOCKED_DOCUMENT_STATES = {RequestState.SUBMITTED_FOR_PAYMENT, RequestState.PAID}


@login_required
@staff_or_owner
@require_POST
def request_document_delete(request, pk, travel_request, profile, doc_id):
    """
    Remove a document uploaded by mistake.

    Only the requester clears their own, and only while the request is still
    with them. Once it has gone to finance these files are the evidence for a
    payment, so they stay.
    """
    if travel_request.user_id != request.user.id:
        raise PermissionDenied
    if travel_request.state in LOCKED_DOCUMENT_STATES:
        messages.error(
            request,
            'Documents cannot be removed once the request has gone for payment.',
        )
        return redirect('request_detail', pk=pk)

    doc = get_object_or_404(Document, pk=doc_id, request=travel_request)
    name = doc.original_name
    doc.file.delete(save=False)
    doc.delete()
    audit('document.removed', user=request.user,
          target=f'request {travel_request.pk}', detail=name)
    messages.success(request, f'Removed {name}.')
    return redirect('request_detail', pk=pk)


@login_required
@staff_or_owner
def request_form_printable(request, pk, travel_request, profile):
    """Render a printable signed-form view for the request."""
    return render(request, 'tsp/requests/signed_form.html', {
        'travel_request': travel_request,
        'profile': profile,
    })


@login_required
def document_download(request, doc_id):
    """Download an attached document if the user owns the request or is committee."""
    doc = get_object_or_404(Document, pk=doc_id)
    travel_request = doc.request
    profile = get_profile(request.user)
    is_owner = travel_request.user_id == request.user.id
    is_staff = profile.is_committee or travel_request.event.is_organised_by(request.user)
    if not (is_owner or is_staff):
        raise PermissionDenied

    return FileResponse(
        doc.file.open('rb'),
        as_attachment=True,
        filename=doc.original_name,
    )


@login_required
@group_required(GROUP_TSP, GROUP_FINANCE)
def reports(request):
    """Filterable TSP report with optional CSV export."""
    profile = get_profile(request.user)
    form = ReportFilterForm(request.GET or None)
    qs = TravelRequest.objects.select_related('user__profile', 'event').prefetch_related('expenses', 'payments')

    if form.is_valid():
        if form.cleaned_data.get('date_from'):
            qs = qs.filter(event__start_date__gte=form.cleaned_data['date_from'])
        if form.cleaned_data.get('date_to'):
            qs = qs.filter(event__end_date__lte=form.cleaned_data['date_to'])
        if form.cleaned_data.get('status'):
            qs = qs.filter(state=form.cleaned_data['status'])
        query = (form.cleaned_data.get('q') or '').strip()
        if query:
            qs = qs.filter(
                Q(user__first_name__icontains=query)
                | Q(user__last_name__icontains=query)
                | Q(user__username__icontains=query)
                | Q(user__email__icontains=query)
                | Q(event__name__icontains=query)
            )

    requests_list = qs.order_by('-updated_at')

    total_requests = qs.count()
    # Requests may be approved and paid in different currencies, so these are
    # grouped rather than added together.
    total_approved = sorted(
        Expense.objects.filter(request__in=qs, approved_amount__isnull=False)
        .annotate(report_currency=Coalesce(
            NullIf('approved_currency', Value('')), 'estimated_currency',
        ))
        .values('report_currency')
        .annotate(total=Sum('approved_amount'))
        .values_list('report_currency', 'total')
    )
    travel_request_ct = ContentType.objects.get_for_model(TravelRequest)
    total_paid = sorted(
        PaymentRecord.objects.filter(
            content_type=travel_request_ct,
            object_id__in=qs.filter(state=RequestState.PAID).values_list('pk', flat=True),
            status__in=SETTLED_PAYMENT_STATUSES,
        )
        .values('currency')
        .annotate(total=Sum('amount'))
        .values_list('currency', 'total')
    )

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="tsp-report.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Date', 'Event', 'Location', 'Recipient', 'Country',
            'Status', 'Approved Amount', 'Paid Amount', 'Paid Currency',
            'Payment Method',
        ])
        for req in requests_list:
            payment = req.settled_payment
            paid = payment.amount if payment else ''
            paid_currency = payment.currency if payment else ''
            method = payment.method_label if payment else ''
            # An amount without its currency is not worth exporting.
            approved = '; '.join(f'{amount} {currency}'
                                 for currency, amount in req.totals_approved())
            writer.writerow(spreadsheet_safe([
                req.updated_at.date(),
                req.event.name,
                req.event.location,
                req.user.get_full_name() or req.user.username,
                getattr(req.user.profile, 'country', ''),
                req.get_state_display(),
                approved,
                paid,
                paid_currency,
                method,
            ]))
        return response

    return render(request, 'tsp/reports.html', {
        'form': form,
        'requests_list': _paginate(request, requests_list, 100),
        'profile': profile,
        'total_requests': total_requests,
        'total_approved': total_approved,
        'total_paid': total_paid,
    })


@login_required
def search(request):
    """Search requests and events; staff may also search users."""
    form = SearchForm(request.GET or None)
    query = ''
    event_table = None
    user_table = None
    requests_found = None
    profile = get_profile(request.user)

    if form.is_valid():
        query = (form.cleaned_data.get('q') or '').strip()

    if query:
        # The committee and finance work across every request; anyone else
        # only ever finds their own.
        visible = TravelRequest.objects.select_related('event', 'user')
        if not (profile.is_tsp or profile.is_finance):
            visible = visible.filter(user=request.user)
        # A reference such as TSP643 names one request, so go straight to it.
        number = re.fullmatch(r'(?:tsp)?\s*#?\s*(\d+)', query, re.IGNORECASE)
        if number:
            match = visible.filter(pk=int(number.group(1))).first()
            if match:
                return redirect('request_detail', pk=match.pk)
        requests_found = visible.filter(
            Q(event__name__icontains=query)
            | Q(talk_title__icontains=query)
            | Q(user__username__icontains=query)
            | Q(user__first_name__icontains=query)
            | Q(user__last_name__icontains=query),
        ).order_by('-updated_at')[:20]

        events = Event.objects.filter(
            Q(name__icontains=query) | Q(location__icontains=query),
        )
        event_table = EventTable(events, prefix='events-')
        RequestConfig(request, paginate={'per_page': 20}).configure(event_table)

        if request.user.is_staff:
            users = User.objects.filter(
                Q(username__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(email__icontains=query),
            ).select_related('profile').prefetch_related('groups')
            user_table = UserTable(users, prefix='users-')
            RequestConfig(request, paginate={'per_page': 20}).configure(user_table)

    return render(request, 'tsp/search.html', {
        'form': form,
        'query': query,
        'requests_found': requests_found,
        'event_table': event_table,
        'user_table': user_table,
        'show_users': request.user.is_staff,
    })


@login_required
@group_required(GROUP_TSP, GROUP_FINANCE)
def audit_log(request):
    """The append-only record of what everyone did, newest first, for staff to read."""
    entries = AuditLogEntry.objects.select_related('user')
    query = request.GET.get('q', '').strip()
    if query:
        entries = entries.filter(
            Q(action__icontains=query) | Q(target__icontains=query)
            | Q(detail__icontains=query) | Q(user__username__icontains=query)
        )
    return render(request, 'tsp/audit_log.html', {
        'entries': _paginate(request, entries, 50),
        'query': query,
    })
