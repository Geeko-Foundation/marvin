"""
Django settings for marvin project.

Sensitive values are read from environment variables (loaded from `.env` via
python-dotenv). Defaults in this file are for local development and tests only.
Copy `.env.example` to `.env` and set production values there.
"""

import os
from pathlib import Path
from .utils import *

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

# --- Security & Core Defaults ---

DEV_SECRET_KEY = 'django-insecure-dev-only-change-me-in-env'
PUBLISHED_SECRET_KEYS = {DEV_SECRET_KEY, 'change-me-to-a-long-random-string'}
LOCAL_HOSTS = {'localhost', '127.0.0.1', '[::1]', '0.0.0.0', 'testserver'}

SECRET_KEY = env('DJANGO_SECRET_KEY', DEV_SECRET_KEY)
DEBUG = env_bool('DEBUG', default=True)
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver')
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS', '')


# --- Application ---

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_tables2',
    'webpush',
    'django_otp',
    'django_otp.plugins.otp_totp',
    'django_otp.plugins.otp_static',
    'payments',
    'profile',
    'tsp',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django_otp.middleware.OTPMiddleware',
    'profile.two_factor.TwoFactorMiddleware',
    'marvin.audit.AuditUserMiddleware',
    'tsp.middleware.RequesterOnboardingMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'marvin.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'profile.context_processors.user_profile',
                'marvin.branding.branding',
            ],
        },
    },
]

WSGI_APPLICATION = 'marvin.wsgi.application'


# --- Database ---

DATABASES = {
    'default': {
        'ENGINE': env('DATABASE_ENGINE', 'django.db.backends.sqlite3'),
        'NAME': env('DATABASE_NAME', str(BASE_DIR / 'db.sqlite3')),
        # Ignored by SQLite, needed by every networked database.
        'USER': env('DATABASE_USER'),
        'PASSWORD': env('DATABASE_PASSWORD'),
        'HOST': env('DATABASE_HOST'),
        'PORT': env('DATABASE_PORT'),
        'CONN_MAX_AGE': int(env('DATABASE_CONN_MAX_AGE', '0')),
    }
}

if env('DATABASE_URL'):
    DATABASES['default'].update(parse_database_url(env('DATABASE_URL')))


# --- Auth ---

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = '/admin/login/'
# The name authenticator apps show next to the six-digit code.
OTP_TOTP_ISSUER = env('BRAND_APP_NAME', 'Marvin')
TWO_FACTOR_ENFORCED = env_bool('TWO_FACTOR_ENFORCED', default=False)
APP_LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'


# --- i18n ---

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
FORMAT_MODULE_PATH = ['marvin.formats']


# --- Static / media ---

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Serve static files straight from app/static dirs; no collectstatic needed.
WHITENOISE_USE_FINDERS = True

MEDIA_URL = 'uploads/'
# Receipts and signed forms live here. On a host whose disk is replaced on
# every deploy, such as Railway, point this at a mounted volume.
MEDIA_ROOT = Path(env('MEDIA_ROOT') or BASE_DIR / 'uploads')

DJANGO_TABLES2_TEMPLATE = 'django_tables2/daisyui.html'


# --- Payments ---

PAYMENTS_GATEWAYS = [
    'payments.gateways.wise.WiseApiGateway',
    'payments.gateways.wise.WiseCsvGateway',
    'payments.gateways.manual.ManualTransferGateway',
]
PAYMENTS_ADAPTERS = [
    'tsp.payables.TravelRequestPayableAdapter',
]
PAYMENTS_DEFAULT_ADAPTER = 'tsp.travelrequest'
PAYMENTS_PERMISSION_CHECK = 'profile.permissions.finance_can_process_payments'


# --- Web Push (django-webpush) ---

# Development VAPID keys. Generate your own for production, see .env.example.
_DEV_VAPID_PUBLIC_KEY = (
    'BF8UddgDIQ_Y5lfOLbOqFLyb5inEudxst2ynx6tM5G7VyPMZwBn0JjpmOfbAdfKcvpVqYZXm5D1wRP-qZVPubu4'
)
_DEV_VAPID_PRIVATE_KEY = (
    '-----BEGIN PRIVATE KEY-----\n'
    'MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg0D5XI7xtdhp0G5SH\n'
    '7AqGGj0USFWgre8aQWVSHfnGmxihRANCAARfFHXYAyEP2OZXzi2zqhS8m+YpxLnc\n'
    'bLdsp8erTORu1cjzGcAZ9CY6Zjn2wHXynL6VamGV5uQ9cET/qmVT7m7u\n'
    '-----END PRIVATE KEY-----\n'
)


WEBPUSH_APP_BASE_URL = env('WEBPUSH_APP_BASE_URL', 'http://127.0.0.1:8000')
WEBPUSH_SETTINGS = {
    'VAPID_PUBLIC_KEY': env('VAPID_PUBLIC_KEY', _DEV_VAPID_PUBLIC_KEY),
    'VAPID_PRIVATE_KEY': normalize_vapid_private_key(env('VAPID_PRIVATE_KEY') or _DEV_VAPID_PRIVATE_KEY),
    'VAPID_ADMIN_EMAIL': env('VAPID_ADMIN_EMAIL', 'admin@marvin.local'),
}


# --- Wise API ---

# Development sandbox token. Set WISE_API_TOKEN in .env for production.
WISE_API_TOKEN = env(
    'WISE_API_TOKEN',
    'bdde76cb-fe9b-4664-974b-c7b52f66294d',
)
WISE_API_PROFILE_ID = env('WISE_API_PROFILE_ID', '')
WISE_API_SANDBOX = env_bool('WISE_API_SANDBOX', default=True)
WISE_SOURCE_CURRENCY = env('WISE_SOURCE_CURRENCY', 'EUR')

# Wise signs webhook bodies with an RSA key it publishes per environment.
# Without the key nothing delivered can be trusted, so the endpoint refuses.
WISE_WEBHOOK_PUBLIC_KEY = env('WISE_WEBHOOK_PUBLIC_KEY', '').replace('\\n', '\n')

# Currencies the committee approves in, which are the ones finance pays from.
PAYOUT_CURRENCIES = env_list('PAYOUT_CURRENCIES', 'EUR,USD,GBP')

# Approximate conversions. Today's rate comes from ExchangeRate-API when a key
# is set; past days, needed for the 90-day average, come from the European
# Central Bank's free reference rates either way.
EXCHANGE_RATE_API_KEY = env('EXCHANGE_RATE_API_KEY')
EXCHANGE_RATE_API_URL = env('EXCHANGE_RATE_API_URL', 'https://v6.exchangerate-api.com/v6')
EXCHANGE_RATES_URL = env('EXCHANGE_RATES_URL', 'https://api.frankfurter.dev/v1')
EXCHANGE_RATES_FETCH = env_bool('EXCHANGE_RATES_FETCH', default=False)


# --- Logging ---

LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'wise': {
            'format': '[{asctime}] {levelname} {name}: {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'wise_console': {
            'class': 'logging.StreamHandler',
            'formatter': 'wise',
        },
        # Kept to about 25 MB. On a host that replaces its disk per deploy,
        # the console copy, which the host keeps, is the one that survives.
        'wise_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_DIR / 'wise.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 4,
            'formatter': 'wise',
        },
    },
    'loggers': {
        'marvin.wise': {
            'handlers': ['wise_console', 'wise_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}


# How long after an event someone may still claim. An organisation can set
# its own; an individual event can override it.
REIMBURSEMENT_WINDOW_DAYS = int(env('REIMBURSEMENT_WINDOW_DAYS', '30'))


# --- Branding ---
# Everything a fork needs to change to make this its own.

BRAND_APP_NAME = env('BRAND_APP_NAME', 'Marvin')
BRAND_ORG_NAME = env('BRAND_ORG_NAME', 'Geeko Foundation')
BRAND_TAGLINE = env('BRAND_TAGLINE', 'Travel Support Program')
# Marvin's own joke. A fork sets this empty to drop the line.
BRAND_STRAPLINE = env('BRAND_STRAPLINE', 'The Paranoid Android')

# Any daisyUI theme name: https://daisyui.com/docs/themes/
BRAND_THEME_LIGHT = env('BRAND_THEME_LIGHT', 'corporate')
BRAND_THEME_DARK = env('BRAND_THEME_DARK', 'business')


# --- Email ---

EMAIL_BACKEND = env('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = env('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(env('EMAIL_PORT', '587'))
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', default=True)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', 'marvin@localhost')

# Where links in notification emails point.
SITE_BASE_URL = env('SITE_BASE_URL', env('WEBPUSH_APP_BASE_URL', 'http://127.0.0.1:8000'))
