from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Set every user password to "demo" (development only)'

    def handle(self, *args, **options):
        count = 0
        for user in User.objects.all():
            user.set_password('demo')
            user.save(update_fields=['password'])
            count += 1
            self.stdout.write(f'  {user.username}')
        self.stdout.write(self.style.SUCCESS(f'Reset password for {count} user(s).'))
