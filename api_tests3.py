#!/usr/bin/env python3
"""
Wise sandbox transfer test - thin wrapper around Marvin's integrated client.

Original functional test (V0.3) exercised the full quote → recipient → transfer
→ sandbox simulation flow. This script delegates to ``payments.gateways.wise_api``.

Usage:
    cd /path/to/marvin
    .venv/bin/python api_tests3.py

Environment (optional - defaults match marvin/settings.py sandbox):
    WISE_API_TOKEN
    WISE_API_PROFILE_ID
    WISE_API_SANDBOX=true
    WISE_SOURCE_CURRENCY=GBP
"""

from __future__ import annotations

import datetime
import os
import sys
from decimal import Decimal

# Allow running as standalone script without manage.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'marvin.settings')

import django

django.setup()

from django.conf import settings

from payments.gateways.wise_api import WiseApiError, get_wise_client

SOURCE_CURRENCY = os.environ.get('WISE_SOURCE_CURRENCY', getattr(settings, 'WISE_SOURCE_CURRENCY', 'GBP'))
TARGET_CURRENCY = os.environ.get('TARGET_CURRENCY', 'EUR')
SOURCE_AMOUNT = Decimal(os.environ.get('SOURCE_AMOUNT', '25.00'))

RECIPIENT_CURRENCY = os.environ.get('RECIPIENT_CURRENCY', 'EUR')
RECIPIENT_TYPE = os.environ.get('RECIPIENT_TYPE', 'iban')
RECIPIENT_NAME = os.environ.get('RECIPIENT_NAME', 'Jane Doe')
RECIPIENT_IBAN = os.environ.get('RECIPIENT_IBAN', 'BE57967747682935')
RECIPIENT_BIC = os.environ.get('RECIPIENT_BIC', 'TRWIBEB1')

REFERENCE_TEXT = f'{os.environ.get("USER", "marvin")} {datetime.datetime.now().strftime("%Y%m%d")}'


def main(transfer_id: int | None = None) -> None:
    client = get_wise_client()
    profile_id = client.resolve_profile_id()
    print(f'Using Wise {client.mode_label} API, profile {profile_id}')

    if transfer_id:
        print(f'Resuming transfer {transfer_id} via sandbox simulation...')
        transfer = client.advance_sandbox_transfer(transfer_id, delay_seconds=2.0)
        print(f'Transfer status: {transfer.get("status")}')
        return

    balances = client.list_balances(types='STANDARD')
    for balance in balances:
        amount = balance.get('amount', {})
        print(f"{balance['currency']} | balanceId={balance['id']} | amount={amount.get('value')}")
    print('---------------------------\n')

    quote = client.create_quote(
        source_currency=SOURCE_CURRENCY,
        target_currency=TARGET_CURRENCY,
        source_amount=float(SOURCE_AMOUNT),
    )
    print(
        'Quote:',
        quote['id'],
        '| payOut:',
        quote.get('payOut'),
        '| rate:',
        quote.get('rate'),
    )

    payee = client.create_payee(
        full_name=RECIPIENT_NAME,
        currency=RECIPIENT_CURRENCY,
        account_type=RECIPIENT_TYPE,
        country='BE',
        address_line='Rue Example 1',
        city='Brussels',
        post_code='1000',
        iban=RECIPIENT_IBAN,
        bic=RECIPIENT_BIC,
    )
    account_id = payee['id']
    print('Created recipient:', account_id)

    transfer = client.create_transfer(
        quote_id=quote['id'],
        target_account=account_id,
        reference=REFERENCE_TEXT,
    )
    transfer_id = transfer['id']
    print('Transfer created:', transfer_id, '| status:', transfer.get('status'))

    outcome = client.fund_or_advance_transfer(transfer_id, sandbox_delay_seconds=2.0)
    print(f"Funding method: {outcome['method']}")
    print('Final status:', outcome['transfer'].get('status'))


if __name__ == '__main__':
    try:
        resume_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
        main(resume_id)
    except WiseApiError as exc:
        print(f'Wise API error ({exc.status_code}): {exc}', file=sys.stderr)
        sys.exit(1)
