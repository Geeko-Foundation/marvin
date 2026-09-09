"""Registry for payment gateways and payable adapters."""

from __future__ import annotations

import importlib

from django.conf import settings
from django.utils.module_loading import import_string

from payments.base import PayableAdapter, PaymentGateway

_gateways: dict[str, PaymentGateway] = {}
_adapters: dict[str, PayableAdapter] = {}


def _load_class(path: str):
    return import_string(path)()


def register_gateway(gateway: PaymentGateway) -> None:
    _gateways[gateway.gateway_id] = gateway


def register_adapter(adapter: PayableAdapter) -> None:
    _adapters[adapter.adapter_id] = adapter


_loaded = False


def load_plugins() -> None:
    """Load gateways and adapters from settings. Idempotent."""
    global _loaded
    if _loaded:
        return

    for path in getattr(settings, 'PAYMENTS_GATEWAYS', []):
        gateway = _load_class(path)
        register_gateway(gateway)

    for path in getattr(settings, 'PAYMENTS_ADAPTERS', []):
        adapter = _load_class(path)
        register_adapter(adapter)

    _loaded = True


def get_gateway(gateway_id: str) -> PaymentGateway | None:
    load_plugins()
    return _gateways.get(gateway_id)


def get_all_gateways() -> list[PaymentGateway]:
    load_plugins()
    return list(_gateways.values())


def get_adapter(adapter_id: str) -> PayableAdapter | None:
    load_plugins()
    return _adapters.get(adapter_id)


def get_all_adapters() -> list[PayableAdapter]:
    load_plugins()
    return list(_adapters.values())


def get_default_adapter() -> PayableAdapter:
    load_plugins()
    adapter_id = getattr(settings, 'PAYMENTS_DEFAULT_ADAPTER', None)
    if adapter_id and adapter_id in _adapters:
        return _adapters[adapter_id]
    if len(_adapters) == 1:
        return next(iter(_adapters.values()))
    if not _adapters:
        raise RuntimeError('No payment adapters registered. Set PAYMENTS_ADAPTERS in settings.')
    raise RuntimeError(
        'Multiple payment adapters registered. Set PAYMENTS_DEFAULT_ADAPTER in settings.'
    )


def check_payment_permission(user) -> bool:
    """Check if user can process payments via configured callback."""
    callback_path = getattr(
        settings,
        'PAYMENTS_PERMISSION_CHECK',
        'payments.defaults.allow_staff',
    )
    callback = import_string(callback_path)
    return callback(user)
