"""
Approximate currency conversion from daily rates.

Past days come from Frankfurter, which republishes the European Central
Bank's daily reference rates for free. When ``EXCHANGE_RATE_API_KEY`` is set,
today's rate comes from ExchangeRate-API instead, whose free plan has no
history. Rates are stored per day against the euro, so any pair is the ratio
of two stored rates. These figures are for guidance: the amount Wise actually
pays out is set by its own quote at payment time.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from tsp.models import ExchangeRate

logger = logging.getLogger(__name__)

AVERAGE_DAYS = 90
# Reference rates skip weekends and holidays, so a few days old is current.
STALE_AFTER_DAYS = 4
REFRESH_CACHE_KEY = 'exchange-rates-refreshed'
REFRESH_CACHE_SECONDS = 6 * 60 * 60
TIMEOUT_SECONDS = 5


@dataclass
class RateSummary:
    """How one unit of ``source`` converts to ``target``, now and on average."""

    source: str
    target: str
    current: Decimal
    current_date: date
    average: Decimal
    low: Decimal
    high: Decimal
    window_start: date
    window_end: date


def supported_currencies():
    from tsp.forms import CURRENCY_CHOICES
    return [code for code, _ in CURRENCY_CHOICES]


def refresh(start, end):
    """Fetch and store the euro rates for every day from ``start`` to ``end``."""
    symbols = ','.join(c for c in supported_currencies() if c != 'EUR')
    url = f'{settings.EXCHANGE_RATES_URL}/{start.isoformat()}..{end.isoformat()}'
    try:
        response = requests.get(
            url, params={'base': 'EUR', 'symbols': symbols}, timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        days = response.json().get('rates', {})
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Could not fetch exchange rates: %s', exc)
        return 0

    return sum(
        _store(date.fromisoformat(day), rates, 'ecb') for day, rates in days.items()
    )


def refresh_latest():
    """Store today's rates from ExchangeRate-API. Does nothing without a key."""
    key = settings.EXCHANGE_RATE_API_KEY
    if not key:
        return 0
    try:
        response = requests.get(
            f'{settings.EXCHANGE_RATE_API_URL}/{key}/latest/EUR', timeout=TIMEOUT_SECONDS,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        # The URL carries the key, so log the kind of failure, never the URL.
        logger.warning('Could not fetch today\'s exchange rates: %s', type(exc).__name__)
        return 0
    if data.get('result') != 'success':
        logger.warning('ExchangeRate-API refused the request: %s', data.get('error-type'))
        return 0

    updated = datetime.fromtimestamp(data['time_last_update_unix'], tz=dt_timezone.utc)
    wanted = set(supported_currencies()) - {'EUR'}
    rates = {c: r for c, r in data.get('conversion_rates', {}).items() if c in wanted}
    return _store(updated.date(), rates, 'exchangerate-api')


def _store(day, rates, source):
    for currency, rate in rates.items():
        ExchangeRate.objects.update_or_create(
            date=day, currency=currency,
            defaults={'rate': Decimal(str(rate)), 'source': source},
        )
    return len(rates)


def ensure_recent():
    """Top the stored rates up to today, at most once every few hours."""
    if not settings.EXCHANGE_RATES_FETCH or cache.get(REFRESH_CACHE_KEY):
        return
    today = timezone.localdate()
    latest = ExchangeRate.objects.filter(source='ecb').order_by('-date').values_list(
        'date', flat=True,
    ).first()
    if latest is None or latest < today - timedelta(days=STALE_AFTER_DAYS):
        start = today - timedelta(days=AVERAGE_DAYS + 10)
        if latest is not None:
            start = max(start, latest + timedelta(days=1))
        refresh(start, today)
    refresh_latest()
    cache.set(REFRESH_CACHE_KEY, True, REFRESH_CACHE_SECONDS)


def window_end_for(event):
    """The average looks back from today, or from the event if it has started."""
    today = timezone.localdate()
    return min(today, event.start_date) if event else today


def _daily_cross_rates(source, target, start, end):
    """``{day: rate}`` for one unit of ``source`` in ``target``, oldest first."""
    if source == target:
        return {}
    wanted = {source, target} - {'EUR'}
    by_day = {}
    for day, currency, rate in ExchangeRate.objects.filter(
        date__gte=start, date__lte=end, currency__in=wanted,
    ).values_list('date', 'currency', 'rate'):
        by_day.setdefault(day, {'EUR': Decimal(1)})[currency] = rate
    return {
        day: rates[target] / rates[source]
        for day, rates in sorted(by_day.items())
        if source in rates and target in rates
    }


def summary(source, target, end=None, days=AVERAGE_DAYS):
    """Current and averaged rate for a pair, or None when no rates are stored."""
    if source == target:
        return None
    end = end or timezone.localdate()
    start = end - timedelta(days=days)
    daily = _daily_cross_rates(source, target, start, end)
    if not daily:
        return None
    values = list(daily.values())
    current_date = max(daily)
    return RateSummary(
        source=source,
        target=target,
        current=daily[current_date],
        current_date=current_date,
        average=sum(values) / len(values),
        low=min(values),
        high=max(values),
        window_start=start,
        window_end=end,
    )


def lock_approval_rate(expense, end):
    """Fix the averaged rate from the approved currency back to the requested one."""
    if not expense.approved_currency or expense.approved_currency == expense.estimated_currency:
        expense.approval_rate = None
        expense.approval_rate_date = None
        return
    rates = summary(expense.approved_currency, expense.estimated_currency, end)
    expense.approval_rate = rates.average.quantize(Decimal('0.00000001')) if rates else None
    expense.approval_rate_date = end if rates else None
