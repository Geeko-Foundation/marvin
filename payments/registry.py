"""Registry for payment gateways and payable adapters."""

from __future__ import annotations

import importlib

from django.conf import settings
from django.utils.module_loading import import_string

from payments.base import PayableAdapter, PaymentGateway

_gateways: dict[str, PaymentGateway] = {}
_adapters: dict[str, PayableAdapter] = {}


def _load_class(path: str):
    """Instantiate a plugin class from its dotted import path."""
    return import_string(path)()


def register_gateway(gateway: PaymentGateway) -> None:
    """Add a gateway instance to the in-memory registry keyed by ``gateway_id``."""
    _gateways[gateway.gateway_id] = gateway


def register_adapter(adapter: PayableAdapter) -> None:
    """Add a payable adapter instance to the in-memory registry keyed by ``adapter_id``."""
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
    """Return the registered gateway for ``gateway_id``, or ``None`` if unknown."""
    load_plugins()
    return _gateways.get(gateway_id)


def get_all_gateways() -> list[PaymentGateway]:
    """Return every gateway loaded from ``settings.PAYMENTS_GATEWAYS``."""
    load_plugins()
    return list(_gateways.values())


def get_adapter(adapter_id: str) -> PayableAdapter | None:
    """Return the registered adapter for ``adapter_id``, or ``None`` if unknown."""
    load_plugins()
    return _adapters.get(adapter_id)


def get_all_adapters() -> list[PayableAdapter]:
    """Return every adapter loaded from ``settings.PAYMENTS_ADAPTERS``."""
    load_plugins()
    return list(_adapters.values())


def get_default_adapter() -> PayableAdapter:
    """
    Return the adapter named by ``settings.PAYMENTS_DEFAULT_ADAPTER``.

    If only one adapter is registered, that adapter is returned. Raises
    ``RuntimeError`` when no adapters exist or when multiple are registered
    without an explicit default.
    """
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
    """
    Return whether ``user`` may access payment views and process reimbursements.

    Delegates to the callable at ``settings.PAYMENTS_PERMISSION_CHECK``.
    """
    callback_path = getattr(
        settings,
        'PAYMENTS_PERMISSION_CHECK',
        'payments.defaults.allow_staff',
    )
    callback = import_string(callback_path)
    return callback(user)
