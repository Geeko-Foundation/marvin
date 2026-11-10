from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand

from profile.groups import ensure_groups

DEMO_PASSWORD = 'demo'


class Command(BaseCommand):
    help = (
        'Load demo users, groups, profiles, and events from tsp/fixtures/initial.json '
        f'and set every user password to "{DEMO_PASSWORD}". '
        'Use --force to flush the database and reload from scratch.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Flush all data and reload the demo fixture (destructive).',
        )

    def handle(self, *args, **options):
        """Load demo fixture when empty (or when forced) and reset all passwords to demo."""
        force = options['force']

        if force:
            self.stdout.write(self.style.WARNING('Flushing database…'))
            call_command('flush', '--noinput', verbosity=0)
        elif User.objects.exists():
            self.stdout.write(self.style.WARNING(
                'Users already exist — skipping fixture load. '
                'Pass --force to flush and reload demo data.'
            ))
            self._set_demo_passwords()
            return

        call_command('loaddata', 'initial', verbosity=1)
        ensure_groups()
        count = self._set_demo_passwords()
        self.stdout.write(self.style.SUCCESS(
            f'Demo data loaded; {count} user(s) set to password "{DEMO_PASSWORD}".'
        ))

    def _set_demo_passwords(self) -> int:
        """Hash and save ``DEMO_PASSWORD`` for every user account."""
        count = 0
        for user in User.objects.all():
            user.set_password(DEMO_PASSWORD)
            user.save(update_fields=['password'])
            count += 1
            self.stdout.write(f'  {user.username}')
        if count:
            self.stdout.write(self.style.SUCCESS(
                f'Password set to "{DEMO_PASSWORD}" for {count} user(s).'
            ))
        return count
