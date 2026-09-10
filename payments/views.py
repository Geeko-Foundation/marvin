from django.contrib import messages
from django.conf import settings

from tsp.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from payments.forms import ProcessPaymentForm
from payments.models import PaymentRecord
from payments.registry import (
    check_payment_permission,
    get_all_gateways,
    get_gateway,
)
from payments.services import PaymentService


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
        queue_items.append(ctx)

    batch_gateways = [gw for gw in get_all_gateways() if gw.supports_batch_export]

    return render(request, 'payments/dashboard.html', {
        'adapter': adapter,
        'queue_items': queue_items,
        'gateways': get_all_gateways(),
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
        messages.success(request, f'Payment processed via {label}.')
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
        content, content_type, filename = service.export_batch(gateway_id)
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
