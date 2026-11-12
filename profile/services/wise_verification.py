"""Step-by-step Wise recipient verification with progress tracking."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from payments.gateways.wise_api import WiseApiError, get_wise_client
from payments.gateways.wise_logging import log_wise_error, log_wise_step
from profile.models import UserProfile
from profile.services.wise_recipient import (
    bank_details_kwargs_from_banking,
    recipient_type_for_method,
    resolve_country,
    resolve_state,
)

VERIFICATION_STEPS = [
    ('create_payee', 'Create Wise payee account'),
    ('add_bank_details', 'Confirm bank details with Wise'),
    ('create_quote', 'Request exchange quote for test transfer'),
    ('create_test_transfer', 'Create test transfer'),
    ('fund_transfer', 'Fund test transfer from Wise balance'),
    ('reverse_transfer', 'Cancel / reverse test transfer'),
    ('finalize', 'Save verification to your profile'),
]

SESSION_KEY = 'wise_verification_state'


class WiseVerificationError(Exception):
    """Raised when a single verification step fails; carries the failing step id."""

    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step = step


def init_verification_state(banking: dict | None = None) -> dict:
    """
    Build empty session state for a multi-step Wise onboarding flow.

    ``banking`` holds form data (IBAN, etc.) only until ``finalize``; it is
    never written to the database as raw account numbers.
    """
    return {
        'account_id': None,
        'quote_id': None,
        'transfer_id': None,
        'steps_done': [],
        'banking': banking or {},
    }


def run_verification_step(profile: UserProfile, state: dict, step_id: str) -> dict:
    """
    Execute one Wise verification step and mutate ``state`` in place.

    Returns step-specific result metadata. Raises ``WiseVerificationError``
    on API or validation failure. Appends ``step_id`` to ``state['steps_done']``.
    """
    if step_id not in [s[0] for s in VERIFICATION_STEPS]:
        raise WiseVerificationError(step_id, f'Unknown step: {step_id}')

    user = profile.user
    client = get_wise_client()
    banking = state.get('banking') or {}
    if not banking:
        raise WiseVerificationError(step_id, 'Banking details missing from session. Go back to banking step.')

    bank = bank_details_kwargs_from_banking(banking)
    recipient_type = recipient_type_for_method(banking.get('banking_method'))
    banking_currency = banking.get('banking_currency') or profile.banking_currency
    source_currency = getattr(settings, 'WISE_SOURCE_CURRENCY', 'GBP')
    label = dict(VERIFICATION_STEPS)[step_id]

    log_wise_step(
        step_id,
        f'Starting: {label}',
        user=user.username,
        mode=client.mode_label,
    )

    try:
        if step_id == 'create_payee':
            payee_country = resolve_country(profile, banking)
            payee = client.create_payee(
                full_name=user.get_full_name() or user.username,
                email=user.email,
                currency=banking_currency,
                account_type=recipient_type,
                country=payee_country,
                address_line=profile.address_line or None,
                city=profile.city or None,
                post_code=profile.postal_code or None,
                state=resolve_state(profile, payee_country),
                **bank,
            )
            state['account_id'] = payee['id']
            result = {'account_id': payee['id']}

        elif step_id == 'add_bank_details':
            account_id = state.get('account_id')
            if not account_id:
                raise WiseVerificationError(step_id, 'Payee account not created yet.')
            account = client.get_payee(account_id)
            result = {'verified': account.get('active', True)}

        elif step_id == 'create_quote':
            account_id = state['account_id']
            quote_kwargs = {
                'source_currency': source_currency,
                'target_currency': banking_currency,
                'target_account': account_id,
            }
            if source_currency == banking_currency:
                quote_kwargs['target_amount'] = Decimal('0.01')
            else:
                quote_kwargs['source_amount'] = Decimal('1.00')
            quote = client.create_quote(**quote_kwargs)
            state['quote_id'] = quote['id']
            result = {'quote_id': quote['id'], 'rate': quote.get('rate')}

        elif step_id == 'create_test_transfer':
            transfer = client.create_transfer(
                quote_id=state['quote_id'],
                target_account=state['account_id'],
                reference=f'VERIFY-{state["account_id"]}',
            )
            state['transfer_id'] = transfer['id']
            result = {'transfer_id': transfer['id'], 'status': transfer['status']}

        elif step_id == 'fund_transfer':
            outcome = client.fund_or_advance_transfer(
                state['transfer_id'],
                sandbox_steps=('processing',),
            )
            transfer = outcome['transfer']
            result = {
                'transfer_id': state['transfer_id'],
                'method': outcome['method'],
                'transfer_status': transfer.get('status'),
            }

        elif step_id == 'reverse_transfer':
            try:
                reversal = client.reverse_transfer(state['transfer_id'])
                reversal_status = reversal.get('status', 'cancelled')
            except WiseApiError as exc:
                if exc.status_code == 409:
                    reversal_status = 'not_cancellable'
                else:
                    raise
            result = {
                'transfer_id': state['transfer_id'],
                'reversal_status': reversal_status,
            }

        elif step_id == 'finalize':
            profile.banking_method = banking.get('banking_method') or profile.banking_method
            profile.banking_currency = banking_currency
            profile.wise_account_id = state['account_id']
            profile.wise_recipient_verified_at = timezone.now()
            profile.save(update_fields=[
                'banking_method',
                'banking_currency',
                'wise_account_id',
                'wise_recipient_verified_at',
            ])
            state.pop('banking', None)
            result = {
                'account_id': state['account_id'],
                'wise_recipient_verified_at': profile.wise_recipient_verified_at.isoformat(),
            }

        else:
            raise WiseVerificationError(step_id, 'Step not implemented.')

    except WiseApiError as exc:
        log_wise_error(step_id, str(exc), user=user.username, status=exc.status_code)
        raise WiseVerificationError(step_id, str(exc)) from exc
    except WiseVerificationError:
        raise
    except Exception as exc:
        log_wise_error(step_id, str(exc), user=user.username)
        raise WiseVerificationError(step_id, str(exc)) from exc

    state['steps_done'] = list(state.get('steps_done', [])) + [step_id]
    log_wise_step(step_id, f'Completed: {label}', user=user.username, **result)
    return result


def setup_wise_recipient(profile: UserProfile, banking: dict) -> dict:
    """
    Run all verification steps sequentially (non-interactive onboarding).

    Used when the full flow can complete in one request rather than step-by-step UI.
    """
    state = init_verification_state(banking)
    last_result = {}
    for step_id, _label in VERIFICATION_STEPS:
        last_result = run_verification_step(profile, state, step_id)
    client = get_wise_client()
    return {
        'account_id': state['account_id'],
        'test_transfer_id': state.get('transfer_id'),
        'mode': client.mode_label,
        **last_result,
    }
