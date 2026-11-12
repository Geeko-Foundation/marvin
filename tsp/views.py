"""TSP views: auth, onboarding, events, travel requests, reports, and search."""

import csv
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Sum, Value
from django.db.models.functions import Coalesce, NullIf
from django.db.models import Q
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from django_tables2 import RequestConfig

from marvin.validators import spreadsheet_safe
from profile.audit_log import AuditLogEntry, record as audit
from profile.decorators import get_profile
from profile.forms import OnboardingPersonalForm, WiseRecipientForm
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP
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
)
from tsp.decorators import group_required, login_required, staff_or_owner
from tsp.forms import (
    ExpenseActualFormSet,
    AcceptForm,
    ApprovalForm,
    CommentForm,
    DocumentUploadForm,
    EventForm,
    ExpenseFormSet,
    LoginForm,
    RejectForm,
    ReportFilterForm,
    SearchForm,
    SignupAccountForm,
    TravelRequestForm,
)
from tsp.tables import EventTable, UserTable
from tsp.models import (
    Document,
    Event,
    Expense,
    RequestState,
    TravelRequest,
    request_owing_money,
)
from tsp.services import exchange_rates
from tsp.services.state_machine import (
    StateTransitionError,
    create_request_with_history,
    transition_request,
)
from payments.models import PaymentRecord
from tsp.payables import TravelRequestPayableAdapter


def _state_badge_class(state):
    """Return a DaisyUI badge CSS class for a travel request state string."""
    mapping = {
        'draft': 'badge-ghost',
        'submitted': 'badge-info',
        'approved': 'badge-warning',
        'rejected': 'badge-error',
        'accepted': 'badge-primary',
        'submitted_for_payment': 'badge-secondary',
        'paid': 'badge-success',
        'cancelled': 'badge-neutral',
    }
    return mapping.get(state, 'badge-ghost')


LOGIN_FAILURE_LIMIT = 10
LOGIN_LOCK_SECONDS = 15 * 60


def login_view(request):
    """Authenticate with username/password; honor ``next`` redirect when present."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST':
        # shortcut: counts live in each process's memory, so N workers allow N
        # times the limit; move CACHES to a shared backend if that matters.
        failures_key = 'login-failures:' + request.POST.get('username', '').strip().lower()
        if cache.get(failures_key, 0) >= LOGIN_FAILURE_LIMIT:
            form.add_error(None, 'Too many failed sign-in attempts for this account. Try again in 15 minutes.')
        elif form.is_valid():
            cache.delete(failures_key)
            login(request, form.get_user())
            next_url = request.POST.get('next') or request.GET.get('next')
            # Only back into this site, never on to a link someone planted.
            if next_url and url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect(settings.LOGIN_REDIRECT_URL)
        else:
            cache.add(failures_key, 0, LOGIN_LOCK_SECONDS)
            cache.incr(failures_key)

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
    """Sign-up wizard step 1: create a requestor account and log in."""
    if request.user.is_authenticated:
        return _signup_redirect_for_user(request.user)

    if request.method == 'POST':
        form = SignupAccountForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, 'Account created. Tell us a bit more about yourself.')
            return redirect('signup_profile')
    else:
        form = SignupAccountForm()

    return render(request, 'tsp/signup/account.html', {
        'form': form,
        'wizard_step': 1,
    })


@login_required
@group_required(GROUP_REQUESTER)
def signup_profile(request):
    """Sign-up wizard step 2: collect required personal and address details."""
    profile = get_profile(request.user)
    if profile.is_wise_recipient_ready():
        return redirect('signup_complete')

    if request.method == 'POST':
        form = OnboardingPersonalForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            form.save(request.user)
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

    if return_to:
        request.session[RETURN_TO_SESSION_KEY] = return_to

    if not profile.is_profile_complete():
        return redirect('signup_profile')

    if profile.is_wise_recipient_ready() and not recreate:
        return redirect('signup_complete')

    if request.method == 'POST':
        form = WiseRecipientForm(request.POST, profile=profile)
        if form.is_valid():
            request.session[SESSION_KEY] = init_verification_state(
                banking=banking_session_payload(form.cleaned_data),
            )
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
        choices = branch_choices(method, bank_code)
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

    if SESSION_KEY not in request.session:
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
    state = request.session.get(SESSION_KEY)

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
    if profile.requires_wise_banking and not profile.is_wise_recipient_ready():
        if not profile.is_profile_complete():
            return redirect('signup_profile')
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
        'action_needed': my_requests.filter(state__in=[
            RequestState.APPROVED, RequestState.ACCEPTED,
        ]),
    }


def _committee_sections(request):
    """Dashboard data for the TSP committee."""
    pending = TravelRequest.objects.filter(
        state=RequestState.SUBMITTED,
    ).select_related('user', 'event').order_by('created_at')

    ninety_days_ago = timezone.now().date() - timezone.timedelta(days=90)
    event_summaries = []
    for event in Event.objects.filter(start_date__gte=ninety_days_ago):
        reqs = event.requests.all()
        event_summaries.append({
            'event': event,
            'total': reqs.count(),
            'by_state': reqs.values('state').annotate(c=Count('id')),
        })

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
    }


def _finance_sections(request):
    """Dashboard data for the finance team."""
    recently_paid = TravelRequest.objects.filter(
        state=RequestState.PAID,
    ).select_related('user', 'event').prefetch_related('payments').order_by('-updated_at')

    travel_request_ct = ContentType.objects.get_for_model(TravelRequest)
    # Only transfers that went through count as paid, one figure per currency.
    paid_total = [
        (row['currency'], row['total'])
        for row in PaymentRecord.objects.filter(
            content_type=travel_request_ct, status__in=('success', 'completed'),
        ).values('currency').annotate(total=Sum('amount')).order_by('-total')
    ]

    with_advance = TravelRequest.objects.filter(
        payments__purpose='advance', refund_settled_at__isnull=True,
    ).distinct().select_related('user', 'event')

    return {
        'payment_queue': TravelRequest.objects.filter(
            state=RequestState.SUBMITTED_FOR_PAYMENT,
        ).select_related('user', 'event').annotate(doc_count=Count('documents')),
        'advance_queue': TravelRequestPayableAdapter().get_queue().filter(state=RequestState.ACCEPTED),
        'owed_back': [r for r in with_advance if r.amount_owed_back()],
        'recently_paid': _paginate(request, recently_paid, 10, 'paid_page'),
        'paid_total': paid_total,
    }


@login_required
def dashboard(request):
    """Home page, with a section for each group the user belongs to."""
    profile = get_profile(request.user)
    if not (profile.is_requester or profile.is_tsp or profile.is_finance):
        raise PermissionDenied

    context = {'profile': profile, 'state_badge': _state_badge_class}
    if profile.is_requester:
        context.update(_requester_sections(request))
    if profile.is_tsp:
        context.update(_committee_sections(request))
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


@login_required
def event_list(request):
    """List TSP events, optionally filtered by start/end date query params."""
    profile = get_profile(request.user)
    date_from = request.GET.get('from')
    date_to = request.GET.get('to')

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
@group_required(GROUP_TSP)
def event_create(request):
    """Create a new TSP event."""
    profile = get_profile(request.user)
    if request.method == 'POST':
        form = EventForm(request.POST)
        if form.is_valid():
            event = form.save()
            audit('event.created', user=request.user, target=event.name,
                  detail=f'{event.start_date} to {event.end_date}'
                         + (f', budget {event.budget} {event.budget_currency}'
                            if event.budget else ''))
            messages.success(request, 'Event created successfully.')
            return redirect('event_list')
    else:
        form = EventForm()

    return render(request, 'tsp/events/form.html', {'form': form, 'profile': profile})


@login_required
@group_required(GROUP_TSP)
def event_edit(request, pk):
    """Edit an existing TSP event."""
    event = get_object_or_404(Event, pk=pk)
    profile = get_profile(request.user)
    if request.method == 'POST':
        form = EventForm(request.POST, instance=event)
        if form.is_valid():
            event = form.save()
            audit('event.updated', user=request.user, target=event.name,
                  detail=', '.join(form.changed_data) or 'no fields changed')
            messages.success(request, 'Event updated.')
            return redirect('event_list')
    else:
        form = EventForm(instance=event)

    return render(request, 'tsp/events/form.html', {'form': form, 'profile': profile, 'event': event})


@login_required
@group_required(GROUP_TSP)
@require_POST
def event_delete(request, pk):
    """Delete an event, as long as nobody has applied to it yet."""
    event = get_object_or_404(Event, pk=pk)
    # Deleting an event cascades to its requests and their payment records,
    # so only an empty one can go. Closing it stops new requests instead.
    if event.requests.exists():
        messages.error(
            request,
            f'{event.name} already has travel requests, so it cannot be deleted. '
            'Close it to stop new requests instead.',
        )
        return redirect('event_list')

    name = event.name
    audit('event.deleted', user=request.user, target=event.name,
          detail=f'{event.start_date} to {event.end_date}')
    event.delete()
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
        if not form.is_valid():
            return JsonResponse({
                'ready': False,
                'needs_recreate': True,
                'message': 'Invalid bank details.',
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
        f'Request #{owing.pk} has {amount} {currency} of its advance to pay back. '
        'New requests can be submitted once finance has confirmed it.',
    )


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
        'show_account_check': profile.is_requester,
        'banking_form': WiseRecipientForm(profile=profile) if profile.is_requester else None,
    })


def _sees_internal_notes(user, profile, travel_request):
    """Committee and finance see internal notes, except on their own request."""
    return profile.is_committee and travel_request.user_id != user.id


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
    if profile.is_tsp and travel_request.state == RequestState.SUBMITTED:
        review_lines = _review_lines(travel_request)
    return render(request, 'tsp/requests/detail.html', {
        'review_lines': review_lines,
        'payout_currencies': settings.PAYOUT_CURRENCIES,
        'travel_request': travel_request,
        'profile': profile,
        'comments': comments,
        'sees_internal_notes': sees_internal,
        'documents': travel_request.documents.select_related('uploaded_by'),
        'state_history': _history_for(travel_request, sees_internal),
        'comment_form': comment_form,
        'doc_form': doc_form,
        'state_badge': _state_badge_class,
        'missing_docs': travel_request.missing_document_kinds(),
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
@group_required(GROUP_TSP)
@require_POST
def request_approve(request, pk):
    """Approve a submitted request and set per-expense approved amounts."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
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
        error = _apply_approved_amounts(travel_request, request.POST)
        if not error:
            error = _set_advance(travel_request, request.POST)
        if not error:
            notes, error = _check_budget(travel_request, request.POST, notes)
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
        try:
            amount = Decimal(data.get(f'approved_amount_{eid}', ''))
        except InvalidOperation:
            continue
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


def _check_budget(travel_request, data, notes):
    """
    Refuse an approval that takes the event past its budget, unless overridden.

    The budget is the committee's own limit, so going past it is allowed but
    has to be deliberate, and the overage is written into the history.
    Returns the notes to record and an error or None.
    """
    event = travel_request.event
    remaining = event.budget_remaining()
    if remaining is None or remaining >= 0:
        return notes, None
    if data.get('over_budget') != '1':
        return notes, (
            f'Approving this would put {event.name} {abs(remaining)} '
            f'{event.budget_currency} over its budget. Approve again with the '
            f'over-budget box ticked to go ahead anyway.'
        )
    overage = f'[over budget by {abs(remaining)} {event.budget_currency}]'
    return (f'{notes} {overage}' if notes else overage), None


@login_required
@group_required(GROUP_TSP)
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

    selected = TravelRequest.objects.filter(
        pk__in=ids, state=RequestState.SUBMITTED,
    ).select_related('event').prefetch_related('expenses')

    reviewed, own, over_budget = 0, 0, []
    for travel_request in selected:
        if travel_request.user_id == request.user.id:
            own += 1
            continue
        with transaction.atomic():
            if action == 'approve':
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
        messages.success(request, f'{reviewed} request{"s" if reviewed != 1 else ""} {action}d.')
    if own:
        messages.warning(
            request,
            f'{own} of the selected requests {"are" if own != 1 else "is"} your own, '
            'so another committee member has to review it.',
        )
    if over_budget:
        messages.warning(
            request,
            f'Not approved, as they would go over the event budget: {", ".join(over_budget)}. '
            'Review them one at a time to approve over budget.',
        )
    ignored = len(ids) - reviewed - own - len(over_budget)
    if ignored:
        messages.warning(
            request,
            f'{ignored} selected request{"s were" if ignored != 1 else " was"} '
            'left alone, having already moved on.',
        )
    return redirect('dashboard')


@login_required
@group_required(GROUP_TSP)
@require_POST
def request_reject(request, pk):
    """Reject a submitted request with required notes."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
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
    """Cancel a request as the owner or TSP committee member."""
    travel_request = get_object_or_404(TravelRequest, pk=pk)
    profile = get_profile(request.user)
    is_owner = travel_request.user_id == request.user.id
    is_tsp = profile.is_tsp

    if not (is_owner or is_tsp):
        raise PermissionDenied

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
            messages.success(request, 'Request accepted. Please upload required documents.')
        except StateTransitionError as e:
            messages.error(request, str(e))
    else:
        messages.error(request, 'All confirmations are required.')
        return redirect('request_accept_page', pk=pk)
    return redirect('request_detail', pk=pk)


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
            return redirect('request_detail', pk=pk)
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
    travel_request = get_object_or_404(TravelRequest, pk=pk)
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
    form = DocumentUploadForm(request.POST, request.FILES)
    if form.is_valid() and request.FILES.get('file'):
        uploaded = request.FILES['file']
        doc = form.save(commit=False)
        doc.request = travel_request
        doc.uploaded_by = request.user
        doc.original_name = uploaded.name
        doc.mimetype = uploaded.content_type or 'application/octet-stream'
        doc.size = uploaded.size
        doc.save()
        messages.success(request, 'Document uploaded.')
    else:
        file_errors = form.errors.get('file')
        messages.error(request, file_errors[0] if file_errors else 'Please select a valid file.')
    return redirect('request_detail', pk=pk)


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
    is_staff = profile.is_committee
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
            paid = req.payment.amount if req.payment else ''
            paid_currency = req.payment.currency if req.payment else ''
            method = req.payment.method_label if req.payment else ''
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
        'state_badge': _state_badge_class,
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
