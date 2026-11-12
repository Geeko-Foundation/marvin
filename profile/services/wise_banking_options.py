"""Bank and branch lists for the methods where Wise supplies the values."""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

from payments.gateways.wise_api import WiseApiError, get_wise_client
from profile.models import BankingMethod
from profile.services.wise_recipient import (
    currency_for_method,
    recipient_type_for_method,
)

# Wise lists 802 banks for Brazil, 130 for Indonesia and 3755 for Japan, and
# the lists change, so they are fetched instead of hardcoded and kept for a day.
CACHE_SECONDS = 60 * 60 * 24

BANK_CODE_METHODS = frozenset({
    BankingMethod.BRAZIL,
    BankingMethod.INDONESIA,
    BankingMethod.JAPAN,
})
BRANCH_CODE_METHODS = frozenset({BankingMethod.JAPAN})


def _requirement_values(method: str, key: str, details: dict | None = None) -> list[tuple[str, str]]:
    """Pull one field's allowed values out of Wise's account requirements."""
    currency = currency_for_method(method)
    if not currency:
        return []
    wise_type = recipient_type_for_method(method)
    payload = {'type': wise_type, 'details': details} if details else None
    try:
        requirements = get_wise_client().account_requirements(
            target_currency=currency,
            source_currency=getattr(settings, 'WISE_SOURCE_CURRENCY', 'GBP'),
            payload=payload,
        )
    except WiseApiError:
        # The signup page still has to render, just without the list.
        return []

    for entry in requirements:
        if entry.get('type') != wise_type:
            continue
        for field in entry.get('fields', []):
            for group in field.get('group', []):
                if group.get('key') == key:
                    return [(v['key'], v['name']) for v in group.get('valuesAllowed') or []]
    return []


def bank_choices(method: str) -> list[tuple[str, str]]:
    """Banks Wise accepts for a method, empty for the methods without a list."""
    if method not in BANK_CODE_METHODS:
        return []
    cache_key = f'wise-banks-{method}'
    choices = cache.get(cache_key)
    if choices is None:
        choices = _requirement_values(method, 'bankCode')
        cache.set(cache_key, choices, CACHE_SECONDS)
    return choices


def branch_choices(method: str, bank_code: str) -> list[tuple[str, str]]:
    """Branches for a chosen bank. Japan lists none until a bank is picked."""
    if method not in BRANCH_CODE_METHODS or not bank_code:
        return []
    cache_key = f'wise-branches-{method}-{bank_code}'
    choices = cache.get(cache_key)
    if choices is None:
        choices = _requirement_values(method, 'branchCode', details={'bankCode': bank_code})
        cache.set(cache_key, choices, CACHE_SECONDS)
    return choices
