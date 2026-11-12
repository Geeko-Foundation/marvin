"""Wise recipient helpers - no local storage of bank account numbers."""

from __future__ import annotations

from payments.gateways.wise_api import WiseApiError, get_wise_client
from profile.countries import CODES_BY_NAME, VALID_CODES, state_code
from profile.models import BankingMethod, UserProfile

RETURN_TO_SESSION_KEY = 'signup_return_to'


# Wise's own account type per method, and the one currency each is valid for.
# Wise calls India 'indian': 'ifsc' is the code inside the details, not an
# account type, and it answers "Account type is not valid".
METHOD_TYPES = {
    BankingMethod.US_BANK: ('aba', 'USD'),
    BankingMethod.INDIA_IFSC: ('indian', 'INR'),
    BankingMethod.AUSTRALIA: ('australian', 'AUD'),
    BankingMethod.BRAZIL: ('brazil', 'BRL'),
    BankingMethod.INDONESIA: ('indonesian', 'IDR'),
    BankingMethod.JAPAN: ('japanese', 'JPY'),
}


# Wise's allowed accountType values, which differ per country.
ACCOUNT_TYPES = {
    BankingMethod.US_BANK: ('CHECKING', 'SAVINGS'),
    BankingMethod.BRAZIL: ('CHECKING', 'SAVINGS', 'PAYMENT'),
    BankingMethod.JAPAN: ('CURRENT', 'SAVINGS', 'CHECKING'),
}

# Each of these currencies has exactly one method Wise accepts for it.
METHOD_FOR_CURRENCY = {currency: method for method, (_, currency) in METHOD_TYPES.items()}


def recipient_type_for_method(method: str | None) -> str:
    """Map a ``BankingMethod`` value to the Wise account ``type`` string."""
    return METHOD_TYPES.get(method, ('iban', None))[0]


def currency_for_method(method: str | None) -> str | None:
    """The only currency Wise accepts for a method, or ``None`` for IBAN."""
    return METHOD_TYPES.get(method, ('iban', None))[1]


def bank_details_kwargs_from_banking(banking: dict) -> dict:
    """
    Extract Wise API keyword arguments from onboarding form data.

    Only the fields required for the selected banking method are included.
    """
    method = banking.get('banking_method')
    if method == BankingMethod.IBAN:
        details = {}
        if banking.get('bank_iban'):
            details['iban'] = banking['bank_iban']
        if banking.get('bank_swift'):
            details['bic'] = banking['bank_swift']
        return details
    account_number = banking.get('bank_account_no')
    if method == BankingMethod.US_BANK:
        return {
            'account_number': account_number,
            'routing_number': banking.get('bank_routing_no'),
            'bank_account_type': banking.get('bank_account_type'),
        }
    if method == BankingMethod.INDIA_IFSC:
        return {
            'account_number': account_number,
            'ifsc': banking.get('bank_ifsc'),
        }
    if method == BankingMethod.AUSTRALIA:
        return {
            'account_number': account_number,
            'bsb_code': banking.get('bank_bsb'),
        }
    if method == BankingMethod.BRAZIL:
        return {
            'account_number': account_number,
            'bank_code': banking.get('bank_code'),
            'branch_code': banking.get('bank_branch'),
            'cpf': banking.get('bank_cpf'),
            'bank_account_type': banking.get('bank_account_type'),
        }
    if method == BankingMethod.INDONESIA:
        return {
            'account_number': account_number,
            'bank_code': banking.get('bank_code'),
        }
    if method == BankingMethod.JAPAN:
        return {
            'account_number': account_number,
            'bank_code': banking.get('bank_code'),
            'branch_code': banking.get('bank_japan_branch'),
            'bank_account_type': banking.get('bank_account_type'),
        }
    return {}


def resolve_country(profile: UserProfile, banking: dict) -> str | None:
    """
    Determine ISO country code for Wise payee creation.

    Prefers a two-letter ``profile.country``, then its spelled out name, then
    the IBAN prefix. Wise answers "Must be ISO 3166-1 alpha-2 country code" for
    anything else, and older profiles hold free text.
    """
    country = (profile.country or '').strip()
    if country.upper() in VALID_CODES:
        return country.upper()
    named = CODES_BY_NAME.get(country.lower())
    if named:
        return named
    iban = banking.get('bank_iban', '')
    if iban and len(iban.replace(' ', '')) >= 2:
        return iban.replace(' ', '')[:2].upper()
    return country or None


def resolve_state(profile: UserProfile, country: str | None = None) -> str | None:
    """The state code for a payee, from the country actually sent to Wise."""
    return state_code(country or profile.country, profile.state)


def banking_session_payload(cleaned_data: dict) -> dict:
    """Serialize validated ``WiseRecipientForm`` data for session storage."""
    return {
        'banking_method': cleaned_data.get('banking_method'),
        'banking_currency': cleaned_data.get('banking_currency'),
        'bank_iban': cleaned_data.get('bank_iban', ''),
        'bank_swift': cleaned_data.get('bank_swift', ''),
        'bank_account_no': cleaned_data.get('bank_account_no', ''),
        'bank_routing_no': cleaned_data.get('bank_routing_no', ''),
        'bank_ifsc': cleaned_data.get('bank_ifsc', ''),
        'bank_account_type': cleaned_data.get('bank_account_type', ''),
        'bank_bsb': cleaned_data.get('bank_bsb', ''),
        'bank_code': cleaned_data.get('bank_code', ''),
        'bank_branch': cleaned_data.get('bank_branch', ''),
        'bank_japan_branch': cleaned_data.get('bank_japan_branch', ''),
        'bank_cpf': cleaned_data.get('bank_cpf', ''),
    }


def mask_account_value(value: str) -> str:
    """Mask an account or IBAN for safe display (first/last four digits visible)."""
    cleaned = (value or '').replace(' ', '')
    if len(cleaned) <= 8:
        return cleaned or '-'
    return f'{cleaned[:4]} **** {cleaned[-4:]}'


def format_payee_summary(payee: dict, profile: UserProfile) -> str:
    """Build a masked, human-readable summary from a Wise payee API response."""
    currency = payee.get('currency') or profile.banking_currency or 'EUR'
    method = profile.banking_method or payee.get('type', 'iban')
    details = payee.get('details') or {}

    if details.get('iban'):
        return f'{currency} · IBAN {mask_account_value(details["iban"])}'
    if details.get('accountNumber'):
        label = 'Account'
        if method == BankingMethod.US_BANK:
            label = 'US account'
        elif method == BankingMethod.INDIA_IFSC:
            label = 'IN account'
        return f'{currency} · {label} {mask_account_value(details["accountNumber"])}'

    holder = payee.get('accountHolderName') or 'Recipient'
    return f'{currency} · {holder}'


def clear_stale_wise_recipient(profile: UserProfile) -> None:
    """Remove Wise account linkage when the remote recipient no longer exists."""
    profile.wise_account_id = None
    profile.wise_recipient_verified_at = None
    profile.save(update_fields=['wise_account_id', 'wise_recipient_verified_at'])


def _account_missing_error(exc: WiseApiError) -> bool:
    """True when Wise indicates the stored account ID is gone or inaccessible."""
    return exc.status_code in (403, 404)


def recreate_wise_recipient(profile: UserProfile, banking: dict) -> dict:
    """
    Re-run full Wise verification and return a status dict from ``check_wise_recipient``.

    On failure, returns ``ready=False`` with an error message instead of raising.
    """
    from profile.services.wise_verification import (
        WiseVerificationError,
        setup_wise_recipient,
    )

    try:
        setup_wise_recipient(profile, banking)
        profile.refresh_from_db()
        result = check_wise_recipient(profile)
        result['recreated'] = True
        result['message'] = (
            'Wise recipient recreated and verified.'
            if result.get('ready')
            else result.get('message', 'Recreation completed but account is not active.')
        )
        return result
    except WiseVerificationError as exc:
        return {
            'ready': False,
            'recreated': False,
            'needs_recreate': True,
            'message': f'Recreation failed: {exc}',
        }


def check_wise_recipient(
    profile: UserProfile,
    *,
    banking: dict | None = None,
    auto_recreate: bool = False,
) -> dict:
    """
    Assess whether the profile's Wise recipient is ready for payouts.

    Returns a dict with ``ready``, ``message``, and optional ``summary``,
    ``account_id``, and ``needs_recreate`` keys. When ``auto_recreate`` is
    True and ``banking`` is supplied, attempts to rebuild a missing account.
    """
    if not profile.wise_account_id:
        if auto_recreate and banking:
            return recreate_wise_recipient(profile, banking)
        return {
            'ready': False,
            'needs_recreate': True,
            'message': 'No Wise recipient is linked. Enter bank details to create one.',
            'summary': profile.banking_summary(),
        }

    if not profile.wise_recipient_verified_at:
        if auto_recreate and banking:
            clear_stale_wise_recipient(profile)
            return recreate_wise_recipient(profile, banking)
        return {
            'ready': False,
            'needs_recreate': True,
            'account_id': profile.wise_account_id,
            'message': 'Wise recipient verification is incomplete. Recreate to continue.',
            'summary': profile.banking_summary(),
        }

    try:
        client = get_wise_client()
        payee = client.get_payee(profile.wise_account_id)
        active = bool(payee.get('active', True))
        summary = format_payee_summary(payee, profile)
        return {
            'ready': active,
            'account_id': profile.wise_account_id,
            'active': active,
            'summary': summary,
            'currency': payee.get('currency') or profile.banking_currency,
            'account_holder': payee.get('accountHolderName', ''),
            'verified_at': profile.wise_recipient_verified_at.isoformat(),
            'message': 'Wise recipient is active and ready for payments.' if active else 'Wise recipient is inactive.',
        }
    except WiseApiError as exc:
        if _account_missing_error(exc):
            stale_id = profile.wise_account_id
            clear_stale_wise_recipient(profile)
            if auto_recreate and banking:
                result = recreate_wise_recipient(profile, banking)
                result['stale_account_id'] = stale_id
                return result
            return {
                'ready': False,
                'needs_recreate': True,
                'stale_account_id': stale_id,
                'message': (
                    f'Wise recipient #{stale_id} was not found. '
                    'Re-enter bank details to recreate your account.'
                ),
                'summary': profile.banking_summary(),
            }
        return {
            'ready': False,
            'account_id': profile.wise_account_id,
            'message': str(exc),
            'summary': profile.banking_summary(),
        }
