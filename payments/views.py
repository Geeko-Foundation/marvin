import json
import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from payments.forms import ProcessPaymentForm
from payments.models import PaymentRecord
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


@_require_payment_permission
def dashboard(request):
    """
    Finance payment queue dashboard.

    Lists payable objects from the default adapter, available gateways, and
    gateways that support batch CSV export.
    """
    service = PaymentService()
    adapter = service.adapter
    queue = service.get_queue()

    queue_items = []
    for obj in queue:
        ctx = adapter.get_queue_item_context(obj)
        ctx['object'] = obj
        ctx['object_id'] = obj.pk
        ctx['detail_url'] = adapter.get_detail_url(obj)
        ctx['in_batch'] = service._payments_under_way(obj).filter(status='exported').first()
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
    try:
        record = service.process_payment(
            object_id=form.cleaned_data['object_id'],
            gateway_id=form.cleaned_data['gateway_id'],
            user=request.user,
        )
        gateway = get_gateway(record.gateway_id)
        label = gateway.label if gateway else record.gateway_id
        # Say what actually happened: a refused transfer is not a payment.
        if record.status in ('success', 'completed'):
            messages.success(request, f'Paid {record.reference} via {label}.')
        elif record.status == 'failed':
            reason = record.metadata.get('error') or 'no reason was given'
            messages.error(request, f'{record.reference} was not paid. {label} refused it: {reason}')
        else:
            messages.info(
                request,
                f'{record.reference} was sent via {label} and is waiting to settle. '
                'It leaves the queue once Wise confirms the transfer.',
            )
    except Exception as exc:
        messages.error(request, str(exc))

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
    record = get_object_or_404(PaymentRecord, pk=pk)
    service = PaymentService()
    record = service.refresh_status(record)
    return JsonResponse({
        'id': record.pk,
        'external_id': record.external_id,
        'status': record.status,
    })


@login_required
def gateway_currencies(request, gateway_id):
    """
    JSON endpoint listing ISO currency codes supported by a gateway.

    Returns an empty list when the gateway is unknown.
    """
    gateway = get_gateway(gateway_id)
    if gateway is None:
        return JsonResponse({'currencies': []})
    return JsonResponse({'currencies': gateway.supported_currencies()})


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

    if signing_key() is None:
        logger.error('Wise webhook received but WISE_WEBHOOK_PUBLIC_KEY is not set')
        return JsonResponse({'error': 'Webhook signing key not configured.'}, status=503)

    if not verify_signature(request.body, request.META.get(SIGNATURE_HEADER)):
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
