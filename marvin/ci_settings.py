"""Development settings on PostgreSQL, for CI's test-postgres job.

Locally, create a marvin database owned by a marvin user with password marvin,
then run: DJANGO_SETTINGS_MODULE=marvin.ci_settings python manage.py test
"""

from .settings import *  # noqa: F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'marvin',
        'USER': 'marvin',
        'PASSWORD': 'marvin',
        'HOST': 'localhost',
        'PORT': '5432',
    }
}
