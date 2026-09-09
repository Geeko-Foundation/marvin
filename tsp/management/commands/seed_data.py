from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand

from tsp.groups import ensure_groups


class Command(BaseCommand):
    help = 'Seed demo users and events for Marvin TSP from fixtures/initial.json'

    def handle(self, *args, **options):
        if User.objects.exists():
            self.stdout.write(self.style.WARNING('Users already exist — skipping seed.'))
            return

        call_command('loaddata', 'initial', verbosity=1)
        ensure_groups()
        self.stdout.write(self.style.SUCCESS('Seed data loaded from tsp/fixtures/initial.json'))
