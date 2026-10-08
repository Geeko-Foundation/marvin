"""Wise recipient helpers — no local storage of bank account numbers."""

from __future__ import annotations

from payments.gateways.wise_api import WiseApiError, get_wise_client
from profile.models import BankingMethod, UserProfile


RETURN_TO_SESSION_KEY = 'signup_return_to'


def recipient_type_for_method(method: str | None) -> str:
    """Map a ``BankingMethod`` value to the Wise account ``type`` string."""
    if method == BankingMethod.US_BANK:
        return 'aba'
    if method == BankingMethod.INDIA_IFSC:
        # Wise calls this one 'indian'. 'ifsc' is the code inside the details,
        # not an account type, and Wise rejects it with "Account type is not valid".
        return 'indian'
    return 'iban'


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
    if method == BankingMethod.US_BANK:
        return {
            'account_number': banking.get('bank_account_no'),
            'routing_number': banking.get('bank_routing_no'),
            'us_account_type': banking.get('bank_account_type'),
        }
    if method == BankingMethod.INDIA_IFSC:
        return {
            'account_number': banking.get('bank_account_no'),
            'ifsc': banking.get('bank_ifsc'),
        }
    return {}


def resolve_country(profile: UserProfile, banking: dict) -> str | None:
    """
    Determine ISO country code for Wise payee creation.

    Prefers a two-letter ``profile.country``, then the IBAN prefix.
    """
    country = (profile.country or '').strip()
    if len(country) == 2:
        return country.upper()
    iban = banking.get('bank_iban', '')
    if iban and len(iban.replace(' ', '')) >= 2:
        return iban.replace(' ', '')[:2].upper()
    return country or None


# Wise asks for US states as a two letter code and rejects anything else.
US_STATE_CODES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'armed forces americas': 'AA', 'armed forces europe': 'AE',
    'armed forces pacific': 'AP', 'california': 'CA', 'colorado': 'CO',
    'connecticut': 'CT', 'delaware': 'DE', 'district of columbia': 'DC', 'florida': 'FL',
    'georgia': 'GA', 'guam': 'GU', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL',
    'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA',
    'maine': 'ME', 'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI',
    'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT',
    'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
    'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND',
    'northern mariana islands': 'MP', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'puerto rico': 'PR', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virgin islands': 'VI', 'virginia': 'VA',
    'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}

_US_STATE_CODE_SET = frozenset(US_STATE_CODES.values())


def resolve_state(profile: UserProfile) -> str | None:
    """
    Determine the US state code for Wise payee creation.

    ``profile.state`` is free text, so accept the code or the spelled out name.
    """
    state = (profile.state or '').strip()
    code = US_STATE_CODES.get(state.lower(), state.upper())
    return code if code in _US_STATE_CODE_SET else None


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
    }


def mask_account_value(value: str) -> str:
    """Mask an account or IBAN for safe display (first/last four digits visible)."""
    cleaned = (value or '').replace(' ', '')
    if len(cleaned) <= 8:
        return cleaned or '—'
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
    from profile.services.wise_verification import WiseVerificationError, setup_wise_recipient

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
