"""
Wise REST API client (live and sandbox).

Based on the functional test in ``api_tests3.py``. All HTTP traffic uses
``requests`` against ``https://api.wise-sandbox.com`` (sandbox) or
``https://api.transferwise.com`` (production).

Recipient verification flow
---------------------------
1. :meth:`WiseApiClient.create_payee` — POST /v1/accounts
2. :meth:`WiseApiClient.add_bank_details` — PUT /v1/accounts/{id}
3. Quote → transfer → fund (or sandbox simulation advance)
4. :meth:`WiseApiClient.reverse_transfer` — PUT /v1/transfers/{id}/cancel

In the Wise sandbox, transfer progression after creation uses the official
simulation endpoints (see Wise Simulation API docs).
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

import requests

from payments.gateways.wise_logging import log_wise_request

WISE_LIVE_BASE_URL = 'https://api.transferwise.com'
WISE_SANDBOX_BASE_URL = 'https://api.wise-sandbox.com'

SUPPORTED_CURRENCIES = [
    'EUR', 'USD', 'GBP', 'BRL', 'CZK', 'PLN', 'AUD', 'INR', 'IDR', 'JPY',
]

SANDBOX_SIMULATION_STEPS = (
    'processing',
    'funds_converted',
    'outgoing_payment_sent',
)


class WiseApiError(Exception):
    """Raised when the Wise API returns a non-success response."""

    def __init__(self, message: str, status_code: int = 0, payload: dict | None = None):
        """Attach HTTP status and parsed JSON body for callers and logging."""
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class WiseApiClient:
    """
    Wise REST API client.

    Parameters
    ----------
    api_token:
        Bearer token from Wise.
    profile_id:
        Wise business/personal profile ID. When omitted, the first available
        profile is resolved via GET /v2/profiles (business preferred).
    sandbox:
        When ``True`` (default), use the Wise sandbox host.
    """

    def __init__(
        self,
        api_token: str | None = None,
        profile_id: str | None = None,
        *,
        sandbox: bool = True,
    ):
        if not api_token:
            raise WiseApiError('WISE_API_TOKEN is not configured.')

        self.api_token = api_token
        self.profile_id = str(profile_id) if profile_id else ''
        self.sandbox = sandbox
        self.base_url = WISE_SANDBOX_BASE_URL if sandbox else WISE_LIVE_BASE_URL

        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
        })
        self._resolved_profile_id: str | None = None

    @property
    def mode_label(self) -> str:
        """Human-readable ``sandbox`` or ``live`` for log messages."""
        return 'sandbox' if self.sandbox else 'live'

    def _url(self, path: str) -> str:
        """Join the configured base URL with an API path."""
        return f'{self.base_url}{path}'

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
        allow_error: bool = False,
    ) -> dict | list:
        """
        Issue a JSON request and parse the response.

        Raises ``WiseApiError`` on HTTP 4xx/5xx unless ``allow_error`` is True.
        Logs every request via ``log_wise_request``.
        """
        headers = dict(extra_headers or {})
        response = self._session.request(
            method,
            self._url(path),
            json=payload,
            headers=headers or None,
        )
        log_wise_request(method, path, response.status_code, mode=self.mode_label)

        try:
            data = response.json()
        except ValueError:
            data = {'message': response.text}

        if response.status_code >= 400 and not allow_error:
            if isinstance(data, dict):
                errors = data.get('errors')
                if errors and isinstance(errors, list):
                    message = errors[0].get('message', response.text)
                else:
                    message = data.get('message', data.get('error', response.text))
            else:
                message = response.text
            raise WiseApiError(str(message), response.status_code, data if isinstance(data, dict) else {})

        if response.status_code >= 400:
            return data if isinstance(data, (dict, list)) else {'message': response.text}
        return data if data is not None else {}

    def get_profiles(self) -> list[dict]:
        """GET /v2/profiles — list profiles available to the token."""
        data = self._request('GET', '/v2/profiles')
        if not isinstance(data, list):
            raise WiseApiError('Unexpected profiles response', payload={'response': data})
        return data

    def resolve_profile_id(self) -> str:
        """Return configured profile ID or auto-select from GET /v2/profiles."""
        if self._resolved_profile_id:
            return self._resolved_profile_id

        if self.profile_id and self.profile_id not in ('0', ''):
            self._resolved_profile_id = self.profile_id
            return self._resolved_profile_id

        profiles = self.get_profiles()
        if not profiles:
            raise WiseApiError('No Wise profiles returned for this API token.')

        business = next((p for p in profiles if p.get('type') == 'business'), None)
        chosen = business or profiles[0]
        self._resolved_profile_id = str(chosen['id'])
        self.profile_id = self._resolved_profile_id
        return self._resolved_profile_id

    def list_balances(self, types: str = 'STANDARD') -> list[dict]:
        """
        GET /v4/profiles/{profileId}/balances — list balance accounts for the profile.

        ``types`` filters balance kinds (default ``STANDARD``).
        """
        profile_id = self.resolve_profile_id()
        response = self._session.get(
            self._url(f'/v4/profiles/{profile_id}/balances'),
            params={'types': types},
        )
        log_wise_request('GET', f'/v4/profiles/{profile_id}/balances', response.status_code, mode=self.mode_label)
        try:
            data = response.json()
        except ValueError:
            data = {'message': response.text}
        if response.status_code >= 400:
            message = data.get('message', response.text) if isinstance(data, dict) else response.text
            raise WiseApiError(str(message), response.status_code, data if isinstance(data, dict) else {})
        if isinstance(data, list):
            return data
        return data.get('balances', []) if isinstance(data, dict) else []

    # ------------------------------------------------------------------
    # Account / payee management
    # ------------------------------------------------------------------

    def create_payee(
        self,
        *,
        full_name: str,
        currency: str,
        email: str | None = None,
        legal_type: str = 'PRIVATE',
        account_type: str = 'iban',
        owned_by_customer: bool = True,
        country: str | None = None,
        address_line: str | None = None,
        city: str | None = None,
        post_code: str | None = None,
        state: str | None = None,
        iban: str | None = None,
        bic: str | None = None,
        account_number: str | None = None,
        routing_number: str | None = None,
        ifsc: str | None = None,
        bsb_code: str | None = None,
        bank_code: str | None = None,
        branch_code: str | None = None,
        cpf: str | None = None,
        bank_account_type: str | None = None,
    ) -> dict:
        """
        Create a recipient (payee) account with bank and address details.

        POST /v1/accounts
        """
        profile_id = self.resolve_profile_id()
        resolved_country = (country or self._country_from_iban(iban) or '').upper()

        details: dict[str, Any] = {'legalType': legal_type}
        address: dict[str, str] = {}
        if resolved_country:
            details['country'] = resolved_country
            address['country'] = resolved_country
        if address_line:
            address['firstLine'] = address_line
        if city:
            address['city'] = city
        if post_code:
            address['postCode'] = post_code
        if state:
            # Wise requires a state for US addresses, as a two letter code.
            address['state'] = state
        if address:
            details['address'] = address

        if iban:
            details['iban'] = iban.replace(' ', '')
        if bic:
            details['bic'] = bic
        if account_number:
            details['accountNumber'] = account_number
        if routing_number:
            details['abartn'] = routing_number
        if ifsc:
            details['ifscCode'] = ifsc
        if bsb_code:
            details['bsbCode'] = bsb_code
        if bank_code:
            details['bankCode'] = bank_code
        if branch_code:
            details['branchCode'] = branch_code
        if cpf:
            details['cpf'] = cpf
        if bank_account_type:
            # Required for US, Brazil and Japan, with different values each.
            details['accountType'] = bank_account_type

        payload: dict[str, Any] = {
            'profile': int(profile_id),
            'accountHolderName': full_name,
            'currency': currency,
            'type': account_type,
            'legalType': legal_type,
            'ownedByCustomer': owned_by_customer,
            'details': details,
        }
        if iban:
            payload['iban'] = iban.replace(' ', '')
        if email:
            payload['email'] = email

        headers = {'X-idempotence-uuid': str(uuid.uuid4())}
        return self._request('POST', '/v1/accounts', payload, extra_headers=headers)

    @staticmethod
    def _country_from_iban(iban: str | None) -> str | None:
        """Extract ISO country code from the first two IBAN characters."""
        if not iban or len(iban.replace(' ', '')) < 2:
            return None
        return iban.replace(' ', '')[:2].upper()

    def add_bank_details(
        self,
        account_id: int,
        *,
        iban: str | None = None,
        bic: str | None = None,
        account_number: str | None = None,
        routing_number: str | None = None,
        ifsc: str | None = None,
        bank_details_type: str | None = None,
        country: str | None = None,
    ) -> dict:
        """
        Add or update bank details on an existing payee.

        PUT /v1/accounts/{accountId}
        """
        details: dict[str, Any] = {}
        if iban:
            details['iban'] = iban.replace(' ', '')
        if bic:
            details['bic'] = bic
        if account_number:
            details['accountNumber'] = account_number
        if routing_number:
            details['abartn'] = routing_number
        if ifsc:
            details['ifscCode'] = ifsc

        resolved_country = country or self._country_from_iban(iban)
        if resolved_country:
            details.setdefault('address', {})['country'] = resolved_country.upper()

        payload: dict[str, Any] = {'details': details}
        if bank_details_type:
            payload['type'] = bank_details_type
        return self._request('PUT', f'/v1/accounts/{account_id}', payload)

    def get_payee(self, account_id: int) -> dict:
        """GET /v1/accounts/{accountId}"""
        return self._request('GET', f'/v1/accounts/{account_id}')

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------

    def account_requirements(
        self,
        *,
        target_currency: str,
        source_currency: str = 'GBP',
        payload: dict | None = None,
    ) -> list:
        """
        The fields Wise requires for each account type on a corridor.

        GET /v1/account-requirements, or POST when ``payload`` carries details
        already filled in: some fields only appear once an earlier answer is
        sent back, and Japanese branches only list after a bank is chosen.
        """
        path = (f'/v1/account-requirements?source={source_currency}'
                f'&target={target_currency}&sourceAmount=1000')
        headers = {'Accept-Minor-Version': '1'}
        method = 'GET' if payload is None else 'POST'
        result = self._request(method, path, payload, extra_headers=headers)
        return result if isinstance(result, list) else []

    def create_quote(
        self,
        *,
        source_currency: str,
        target_currency: str,
        target_amount: Decimal | float | None = None,
        source_amount: Decimal | float | None = None,
        target_account: int | None = None,
        preferred_pay_in: str = 'BALANCE',
    ) -> dict:
        """POST /v3/profiles/{profileId}/quotes"""
        profile_id = self.resolve_profile_id()
        payload: dict[str, Any] = {
            'sourceCurrency': source_currency,
            'targetCurrency': target_currency,
            'payOut': 'BANK_TRANSFER',
            'preferredPayIn': preferred_pay_in,
        }
        if target_amount is not None:
            payload['targetAmount'] = float(target_amount)
        if source_amount is not None:
            payload['sourceAmount'] = float(source_amount)
        if target_account is not None:
            payload['targetAccount'] = target_account
        return self._request('POST', f'/v3/profiles/{profile_id}/quotes', payload)

    def create_transfer(
        self,
        *,
        quote_id: str | int,
        target_account: int,
        reference: str,
        customer_transaction_id: str | None = None,
    ) -> dict:
        """POST /v1/transfers"""
        payload = {
            'targetAccount': target_account,
            'quoteUuid': str(quote_id),
            'customerTransactionId': customer_transaction_id or str(uuid.uuid4()),
            'details': {
                'reference': reference,
            },
        }
        return self._request('POST', '/v1/transfers', payload)

    def fund_transfer(self, transfer_id: int, funding_type: str = 'BALANCE') -> dict:
        """
        POST /v3/profiles/{profileId}/transfers/{transferId}/payments

        Handles Wise SCA (403 + One-Time-Token) when approval is already granted.
        """
        profile_id = self.resolve_profile_id()
        path = f'/v3/profiles/{profile_id}/transfers/{transfer_id}/payments'
        body = {'type': funding_type}

        response = self._session.post(self._url(path), json=body)
        log_wise_request('POST', path, response.status_code, mode=self.mode_label)

        if response.status_code == 403:
            approval = response.headers.get('x-2fa-approval-result')
            ott = response.headers.get('x-2fa-approval')
            if approval == 'APPROVED' and ott:
                response = self._session.post(
                    self._url(path),
                    json=body,
                    headers={'One-Time-Token': ott},
                )
                log_wise_request('POST', path, response.status_code, mode=self.mode_label)
            elif ott:
                raise WiseApiError(
                    f'SCA required. Complete Wise challenges for OTT={ott} and retry with One-Time-Token.',
                    status_code=403,
                )
            else:
                raise WiseApiError(
                    'Transfer funding requires manual approval in Wise (UK/EU non-bank). '
                    'Log in to Wise to fund this transfer, or use sandbox simulation.',
                    status_code=403,
                )

        try:
            data = response.json()
        except ValueError:
            data = {'message': response.text}

        if response.status_code >= 400:
            message = data.get('message', response.text) if isinstance(data, dict) else response.text
            raise WiseApiError(str(message), response.status_code, data if isinstance(data, dict) else {})

        return data

    def simulate_sandbox_transfer_step(self, transfer_id: int, step: str) -> dict:
        """
        Advance a sandbox transfer via Wise Simulation API.

        GET /v1/simulation/transfers/{transferId}/{step}
        """
        if not self.sandbox:
            raise WiseApiError('Sandbox simulation is only available in sandbox mode.')
        path = f'/v1/simulation/transfers/{transfer_id}/{step}'
        return self._request('GET', path, allow_error=True)

    def advance_sandbox_transfer(
        self,
        transfer_id: int,
        *,
        delay_seconds: float = 2.0,
        steps: tuple[str, ...] | None = None,
    ) -> dict:
        """
        Advance a sandbox transfer via Wise Simulation API.

        Mirrors ``simulate_user()`` from ``api_tests3.py``.
        Pass ``steps`` to run a subset (e.g. only ``processing`` for verification).
        """
        last: dict = {}
        for step in steps or SANDBOX_SIMULATION_STEPS:
            last = self.simulate_sandbox_transfer_step(transfer_id, step)
            if isinstance(last, dict) and last.get('errors'):
                message = last['errors'][0].get('message', f'Simulation step {step} failed')
                raise WiseApiError(message, payload=last)
            time.sleep(delay_seconds)
        return self.get_transfer(transfer_id)

    def fund_or_advance_transfer(
        self,
        transfer_id: int,
        *,
        sandbox_delay_seconds: float = 2.0,
        sandbox_steps: tuple[str, ...] | None = None,
    ) -> dict:
        """
        Fund a transfer from balance, or advance via sandbox simulation.

        UK/EU partners often cannot auto-fund; sandbox uses Wise simulation endpoints.
        """
        try:
            fund = self.fund_transfer(transfer_id)
            return {'method': 'fund', 'fund': fund, 'transfer': self.get_transfer(transfer_id)}
        except WiseApiError as exc:
            if self.sandbox and exc.status_code in (403, 422):
                transfer = self.advance_sandbox_transfer(
                    transfer_id,
                    delay_seconds=sandbox_delay_seconds,
                    steps=sandbox_steps,
                )
                return {'method': 'sandbox_simulation', 'transfer': transfer}
            raise

    def get_transfer(self, transfer_id: int) -> dict:
        """GET /v1/transfers/{transferId}"""
        return self._request('GET', f'/v1/transfers/{transfer_id}')

    def reverse_transfer(self, transfer_id: int) -> dict:
        """
        Cancel / reverse a transfer.

        PUT /v1/transfers/{transferId}/cancel
        """
        return self._request('PUT', f'/v1/transfers/{transfer_id}/cancel')

    # ------------------------------------------------------------------
    # Recipient verification
    # ------------------------------------------------------------------

    def test_recipient(
        self,
        account_id: int,
        *,
        currency: str = 'EUR',
        source_currency: str = 'GBP',
        test_amount: Decimal | float = Decimal('1.00'),
        reference: str | None = None,
    ) -> dict:
        """Send a small test transfer to verify a recipient account."""
        quote = self.create_quote(
            source_currency=source_currency,
            target_currency=currency,
            target_amount=test_amount if source_currency == currency else None,
            source_amount=test_amount if source_currency != currency else None,
            target_account=account_id,
        )
        transfer = self.create_transfer(
            quote_id=quote['id'],
            target_account=account_id,
            reference=reference or f'VERIFY-{account_id}',
        )
        self.fund_or_advance_transfer(
            transfer['id'],
            sandbox_steps=('processing',) if self.sandbox else None,
        )
        return self.get_transfer(transfer['id'])

    def verify_recipient(
        self,
        *,
        full_name: str,
        email: str,
        currency: str,
        account_type: str = 'iban',
        iban: str | None = None,
        bic: str | None = None,
        account_number: str | None = None,
        routing_number: str | None = None,
        ifsc: str | None = None,
        source_currency: str = 'GBP',
        test_amount: Decimal | float = Decimal('1.00'),
        country: str | None = None,
        address_line: str | None = None,
        city: str | None = None,
        post_code: str | None = None,
    ) -> dict:
        """Full recipient verification workflow."""
        payee = self.create_payee(
            full_name=full_name,
            currency=currency,
            email=email,
            account_type=account_type,
            iban=iban,
            bic=bic,
            account_number=account_number,
            routing_number=routing_number,
            ifsc=ifsc,
            country=country or self._country_from_iban(iban),
            address_line=address_line,
            city=city,
            post_code=post_code,
        )
        account_id = payee['id']
        account = payee

        test_transfer = self.test_recipient(
            account_id,
            currency=currency,
            source_currency=source_currency,
            test_amount=test_amount,
        )
        try:
            reversal = self.reverse_transfer(test_transfer['id'])
            reversal_status = reversal.get('status', 'cancelled')
        except WiseApiError as exc:
            reversal_status = 'not_cancellable' if exc.status_code == 409 else None
            if reversal_status is None:
                raise

        return {
            'account_id': account_id,
            'account': account,
            'test_transfer_id': test_transfer['id'],
            'test_transfer_status': test_transfer['status'],
            'reversal_status': reversal_status,
            'verified': account.get('verified', True),
        }

    def get_currencies(self) -> list[str]:
        """GET /v1/currencies — returns supported currency codes."""
        try:
            data = self._request('GET', '/v1/currencies')
            if isinstance(data, list):
                return [item['code'] for item in data]
        except WiseApiError:
            pass
        return SUPPORTED_CURRENCIES


def get_wise_client(**kwargs) -> WiseApiClient:
    """
    Factory that reads Django settings when kwargs are omitted.

    Settings:
        WISE_API_TOKEN
        WISE_API_PROFILE_ID (optional — auto-resolved when empty)
        WISE_API_SANDBOX (default True)
    """
    try:
        from django.conf import settings as django_settings
    except ImportError:
        django_settings = None

    if django_settings:
        kwargs.setdefault('api_token', getattr(django_settings, 'WISE_API_TOKEN', None))
        kwargs.setdefault('profile_id', getattr(django_settings, 'WISE_API_PROFILE_ID', None))
        if 'sandbox' not in kwargs:
            kwargs['sandbox'] = getattr(django_settings, 'WISE_API_SANDBOX', True)

    return WiseApiClient(**kwargs)
