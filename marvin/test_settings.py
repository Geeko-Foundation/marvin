import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent

# Settings are read once at start-up, so each case starts a fresh interpreter
# with only the environment a deployment would have, and no local .env.
PROBE = '''
import os, dotenv
dotenv.load_dotenv = lambda *a, **k: None
import django
try:
    django.setup()
    from django.conf import settings
    print('DEBUG' if settings.DEBUG else 'LIVE', settings.SESSION_COOKIE_SECURE)
except Exception as exc:
    print('REFUSED', type(exc).__name__)
'''


def start(settings_module='marvin.settings', **env):
    clean = {k: v for k, v in os.environ.items()
             if k not in ('DJANGO_SECRET_KEY', 'DEBUG', 'ALLOWED_HOSTS', 'DJANGO_SETTINGS_MODULE')}
    clean['DJANGO_SETTINGS_MODULE'] = settings_module
    clean.update(env)
    result = subprocess.run([sys.executable, '-c', PROBE], cwd=ROOT, env=clean,
                            capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


class ProductionSettingsTests(SimpleTestCase):
    """A site people can reach never runs on a key published in this repository."""

    def test_a_fresh_clone_runs_with_dev_defaults(self):
        self.assertEqual(start(), 'DEBUG False')

    def test_prod_settings_refuses_default_or_missing_secret_key(self):
        for key in (None, '', 'django-insecure-dev-only-change-me-in-env',
                    'change-me-to-a-long-random-string'):
            with self.subTest(key=key):
                env = {'ALLOWED_HOSTS': 'tsp.example.org'}
                if key is not None:
                    env['DJANGO_SECRET_KEY'] = key
                self.assertEqual(start(settings_module='marvin.prod_settings', **env),
                                 'REFUSED ImproperlyConfigured')

    def test_prod_settings_refuses_localhost_only_hosts(self):
        self.assertEqual(
            start(settings_module='marvin.prod_settings',
                  DJANGO_SECRET_KEY='valid-production-secret-key',
                  ALLOWED_HOSTS='localhost,127.0.0.1'),
            'REFUSED ImproperlyConfigured'
        )

    def test_prod_settings_runs_live_with_production_configuration(self):
        self.assertEqual(
            start(settings_module='marvin.prod_settings',
                  DJANGO_SECRET_KEY='valid-production-secret-key',
                  ALLOWED_HOSTS='tsp.example.org'),
            'LIVE True'
        )
