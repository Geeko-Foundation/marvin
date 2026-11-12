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
os.environ['DJANGO_SETTINGS_MODULE'] = 'marvin.settings'
import django
try:
    django.setup()
    from django.conf import settings
    print('DEBUG' if settings.DEBUG else 'LIVE', settings.SESSION_COOKIE_SECURE)
except Exception as exc:
    print('REFUSED', type(exc).__name__)
'''


def start(**env):
    clean = {k: v for k, v in os.environ.items()
             if k not in ('DJANGO_SECRET_KEY', 'DEBUG', 'ALLOWED_HOSTS')}
    clean.update(env)
    result = subprocess.run([sys.executable, '-c', PROBE], cwd=ROOT, env=clean,
                            capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


class ProductionSettingsTests(SimpleTestCase):
    """A site people can reach never runs on a key published in this repository."""

    def test_a_public_site_refuses_a_published_or_missing_key(self):
        for key in (None, '', 'django-insecure-dev-only-change-me-in-env',
                    'change-me-to-a-long-random-string'):
            with self.subTest(key=key):
                env = {'ALLOWED_HOSTS': 'tsp.example.org'}
                if key is not None:
                    env['DJANGO_SECRET_KEY'] = key
                self.assertEqual(start(**env), 'REFUSED ImproperlyConfigured')

    def test_a_public_site_with_its_own_key_runs_live_with_secure_cookies(self):
        self.assertEqual(start(ALLOWED_HOSTS='tsp.example.org', DJANGO_SECRET_KEY='its-own-key'),
                         'LIVE True')

    def test_a_fresh_clone_still_runs_with_no_configuration(self):
        self.assertEqual(start(), 'DEBUG False')
