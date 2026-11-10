from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from tsp.services.exchange_rates import AVERAGE_DAYS, refresh, refresh_latest


class Command(BaseCommand):
    help = (
        'Fetch past daily reference rates, plus today\'s from ExchangeRate-API '
        'when EXCHANGE_RATE_API_KEY is set, for approximate conversions.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=AVERAGE_DAYS + 10,
            help=f'How many days back to fetch (default {AVERAGE_DAYS + 10}).',
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        stored = refresh(today - timedelta(days=options['days']), today) + refresh_latest()
        if stored:
            self.stdout.write(self.style.SUCCESS(f'Stored {stored} daily rates.'))
        else:
            self.stdout.write(self.style.WARNING('No rates fetched; see the log for why.'))
