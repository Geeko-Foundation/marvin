import json
import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from payments.forms import ProcessPaymentForm
from payments.models import PaymentRecord, WiseLogEntry, masked, transfer_id_in
from payments.registry import (
    check_payment_permission,
    get_all_gateways,
    get_gateway,
)
from payments.services import PaymentService
from payments.webhooks import (
    SIGNATURE_HEADER,
    TEST_HEADER,
    signing_key,
    transfer_id_from,
    verify_signature,
)
from tsp.decorators import login_required

logger = logging.getLogger('marvin.wise')


def _require_payment_permission(view_func):
    """
    Decorator: require login plus ``settings.PAYMENTS_PERMISSION_CHECK``.

    Unauthenticated users are redirected to the app login. Authenticated users
    without payment permission receive HTTP 403.
    """
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path(), login_url=settings.APP_LOGIN_URL)
        if not check_payment_permission(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return login_required(wrapper)


PROGRESS_STEPS = ('Ready to pay', 'Sent to Wise', 'Approved in Wise', 'Money sent')
# Where each Wise transfer status sits among the steps above.
_STEP_FOR_TRANSFER = {'processing': 2, 'funds_converted': 2, 'outgoing_payment_sent': 3}


def _progress(sending, in_batch):
    """The payment's steps as (label, reached), for the progress bar on the queue."""
    if sending:
        reached = _STEP_FOR_TRANSFER.get(sending.metadata.get('transfer_status'), 1)
    else:
        reached = 1 if in_batch else 0
    return [(label, i <= reached) for i, label in enumerate(PROGRESS_STEPS)]


@_require_payment_permission
def dashboard(request):
    """
    Finance payment queue dashboard.

    Lists payable objects from the default adapter, available gateways, and
    gateways that support batch CSV export.
    """
    service = PaymentService()
    adapter = service.adapter

    # Ask Wise about transfers still waiting, so a settled one leaves the queue
    # without waiting for a webhook. shortcut: one Wise call per waiting
    # transfer on each visit; fine for a short queue.
    for obj in service.get_queue():
        for record in service._payments_under_way(obj).exclude(status='exported').exclude(external_id=None):
            service.refresh_status(record)

    queue_items = []
    for obj in service.get_queue():
        ctx = adapter.get_queue_item_context(obj)
        ctx['object'] = obj
        ctx['object_id'] = obj.pk
        ctx['detail_url'] = adapter.get_detail_url(obj)
        under_way = service._payments_under_way(obj)
        ctx['in_batch'] = under_way.filter(status='exported').first()
        ctx['sending'] = under_way.exclude(status='exported').first()
        ctx['progress'] = _progress(ctx['sending'], ctx['in_batch'])
        if ctx['sending']:
            status = _status_context(ctx['sending'])
            ctx['status_url'] = status['status_url']
            ctx['status_message'] = status['message']
            ctx['awaiting_approval'] = status['awaiting_approval']
        queue_items.append(ctx)

    batch_gateways = [gw for gw in get_all_gateways() if gw.supports_batch_export]
    # A batch gateway is confirmed per row only once the row is in a file.
    single_gateways = [gw for gw in get_all_gateways() if not gw.supports_batch_export]

    return render(request, 'payments/dashboard.html', {
        'adapter': adapter,
        'queue_items': queue_items,
        'gateways': single_gateways,
        'batch_gateways': batch_gateways,
        'process_form': ProcessPaymentForm(),
        'wise_sandbox': settings.WISE_API_SANDBOX,
        'wise_url': 'https://wise-sandbox.com/' if settings.WISE_API_SANDBOX else 'https://wise.com/',
        'can_send_to_wise': get_gateway('wise-api') is not None,
    })


@_require_payment_permission
@require_POST
def process_payment(request):
    """
    Process a single payment for one queue item via the selected gateway.

    Expects ``object_id`` and ``gateway_id`` in POST data. Redirects back to
    the dashboard with a success or error message.
    """
    form = ProcessPaymentForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Invalid payment data.')
        return redirect('payments:dashboard')

    service = PaymentService()
    record = None
    try:
        record = service.process_payment(
            object_id=form.cleaned_data['object_id'],
            gateway_id=form.cleaned_data['gateway_id'],
            user=request.user,
        )
        gateway = get_gateway(record.gateway_id)
        label = gateway.label if gateway else record.gateway_id
        # Say what actually happened: a refused transfer is not a payment.
        if record.status in ('success', 'completed') and record.metadata.get('sandbox'):
            level, text = messages.WARNING, f'{record.reference} went through the Wise sandbox. No real money was sent.'
        elif record.status in ('success', 'completed'):
            level, text = messages.SUCCESS, f'Paid {record.reference} via {label}.'
        elif record.status == 'failed':
            reason = record.metadata.get('error') or 'no reason was given'
            level, text = messages.ERROR, f'{record.reference} was not paid. {label} refused it: {reason}'
        elif record.metadata.get('awaiting_approval'):
            level, text = messages.INFO, f'{record.reference}: approval sent to Wise. Approve it in Wise and it shows as paid here.'
        elif record.metadata.get('error'):
            # The transfer exists but Wise stopped it part way, so say why.
            level, text = messages.WARNING, (
                f'{record.reference} was created in {label} but Wise is holding it: '
                f'{record.metadata["error"]} It stays in the queue and cannot be paid twice.'
            )
        else:
            level, text = messages.INFO, (
                f'{record.reference} was sent via {label} and is waiting to settle. '
                'It leaves the queue once Wise confirms the transfer.'
            )
    except Exception as exc:
        level, text = messages.ERROR, str(exc)

    # The queue page sends this through htmx and swaps in the payment's status.
    if request.headers.get('HX-Request'):
        if record is None:
            return HttpResponse(f'<p class="text-sm text-error mt-3">{escape(text)}</p>')
        return render(request, 'payments/_status.html', _status_context(record, message=text))
    messages.add_message(request, level, text)
    return redirect('payments:dashboard')


@_require_payment_permission
@require_POST
def send_wise_batch(request):
    """Create Wise batch groups for everything waiting, for finance to fund in Wise."""
    try:
        records, problems = PaymentService().send_wise_batch(request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('payments:dashboard')
    if records:
        messages.success(
            request,
            f'Sent {len(records)} payment{"s" if len(records) != 1 else ""} to Wise as a batch. '
            'Open Wise, review the batch and fund it to send them.',
        )
    for problem in problems:
        messages.error(request, f'Not added to the batch. {problem}')
    return redirect('payments:dashboard')


@_require_payment_permission
@require_POST
def batch_export(request):
    """
    Download a batch payment file (e.g. Wise Send-by-email CSV) for the full queue.

    Expects ``gateway_id`` in POST. Returns a file download or redirects with
    an error message on failure.
    """
    gateway_id = request.POST.get('gateway_id')
    if not gateway_id:
        messages.error(request, 'No gateway specified for batch export.')
        return redirect('payments:dashboard')

    service = PaymentService()
    try:
        content, content_type, filename = service.export_batch(gateway_id, request.user)
        messages.info(
            request,
            'Batch file downloaded. Once Wise has processed it, mark each request paid '
            'with Wise Batch CSV. Requests in this file are left out of the next export.',
        )
        response = HttpResponse(content, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as exc:
        messages.error(request, str(exc))
        return redirect('payments:dashboard')


@_require_payment_permission
def payment_status(request, pk):
    """
    JSON endpoint to refresh and return status for one ``PaymentRecord``.

    Used by the dashboard to poll async gateway transfers (e.g. Wise API).
    """
    record = PaymentService().refresh_status(get_object_or_404(PaymentRecord, pk=pk))
    status = _status_context(record)
    if request.headers.get('HX-Request'):
        return render(request, 'payments/_status.html', status)
    return JsonResponse({
        'id': record.pk,
        'external_id': record.external_id,
        'status': record.status,
        'paid': status['paid'],
        'final': status['final'],
        'message': status['message'],
        'progress': [reached for _, reached in status['progress']],
    })


def _status_context(record, message=None):
    """What the payment status fragment shows for ``record``."""
    paid = record.status in ('success', 'completed')
    failed = record.status in ('failed', 'cancelled')
    awaiting = record.status == 'processing' and (
        record.metadata.get('awaiting_approval')
        or record.metadata.get('transfer_status') == 'incoming_payment_waiting'
    )
    if message:
        pass
    elif paid:
        message = f'Paid. Wise confirmed the transfer for {record.reference}.'
    elif failed:
        reason = record.metadata.get('error') or record.metadata.get('transfer_status') or record.status
        message = f'Not paid: {reason}. Reload the page to pay it again.'
    elif record.metadata.get('error'):
        message = f'Waiting for Wise. Wise is holding it: {record.metadata["error"]}'
    elif awaiting:
        batch = record.metadata.get('batch_name')
        message = (f'In the Wise batch "{batch}". ' if batch else '') + 'Approval sent to Wise.'
    else:
        message = f'Waiting for Wise to confirm ({record.get_status_display()}).'
    return {
        'pay_id': record.object_id,
        'status_url': reverse('payments:status', args=[record.pk]),
        'message': message,
        'paid': paid,
        'final': paid or failed,
        'awaiting_approval': awaiting,
        'wise_url': 'https://wise-sandbox.com/' if settings.WISE_API_SANDBOX else 'https://wise.com/',
        'progress': _progress(record, None),
    }


@_require_payment_permission
def gateway_currencies(request, gateway_id):

    """
    JSON endpoint listing ISO currency codes supported by a gateway.

    Returns an empty list when the gateway is unknown.
    """
    gateway = get_gateway(gateway_id)
    if gateway is None:
        return JsonResponse({'currencies': []})
    return JsonResponse({'currencies': gateway.supported_currencies()})


def _log_webhook(request, signed):
    """
    Note every delivery in the Wise log, and keep the body of the signed ones.

    Anyone can post to this address, so an unsigned body is not stored: the
    log would otherwise hold whatever a stranger chose to send, at any size.
    """
    if not signed:
        body = {'unsigned': f'{len(request.body)} bytes, not kept'}
    else:
        try:
            body = json.loads(request.body)
        except ValueError:
            body = {'text': request.body.decode('utf-8', 'replace')[:5000]}
    WiseLogEntry.objects.create(
        kind=WiseLogEntry.WEBHOOK, method='POST', path=request.path[:300],
        request_body=masked(body), transfer_id=transfer_id_in('', body), signature_valid=signed,
    )


@csrf_exempt
@require_http_methods(['GET', 'HEAD', 'POST'])
def wise_webhook(request):
    """
    Let Wise tell us a transfer changed, instead of waiting for a poll.

    The event is only a prompt to look: the transfer's new state is read back
    from Wise through the usual status refresh, so a delivery that is replayed
    or forged cannot by itself mark a request paid.

    Wise retries on any non-2xx, so an event we have no record for is still
    answered 200. Only an unusable request is refused.
    """
    # Wise checks the URL answers before saving it, by visiting it and by
    # sending a test notification. Neither carries anything to act on.
    if request.method != 'POST' or request.META.get(TEST_HEADER, '').lower() == 'true':
        return JsonResponse({'status': 'ok'})

    signed = signing_key() is not None and verify_signature(request.body, request.META.get(SIGNATURE_HEADER))
    _log_webhook(request, signed)

    if signing_key() is None:
        logger.error('Wise webhook received but WISE_WEBHOOK_PUBLIC_KEY is not set')
        return JsonResponse({'error': 'Webhook signing key not configured.'}, status=503)

    if not signed:
        logger.warning('Wise webhook rejected: bad signature')
        return JsonResponse({'error': 'Invalid signature.'}, status=400)

    try:
        event = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'Malformed JSON.'}, status=400)
    if not isinstance(event, dict):
        return JsonResponse({'error': 'Malformed JSON.'}, status=400)

    transfer_id = transfer_id_from(event)
    if transfer_id is None:
        return JsonResponse({'status': 'ignored: not a transfer state change'})

    record = PaymentRecord.objects.filter(
        external_id=transfer_id, gateway_id='wise-api',
    ).first()
    if record is None:
        # The same Wise profile is used for transfers made outside Marvin.
        return JsonResponse({'status': f'ignored: no record for transfer {transfer_id}'})

    record = PaymentService().refresh_status(record)
    logger.info('Wise webhook: transfer %s is %s', transfer_id, record.status)
    return JsonResponse({'status': record.status})
