"""
Production Django settings for marvin project.

Inherits all defaults from standard settings and applies hardened
production security, performance optimizations, and strict environment validation.
"""

from .settings import *
from .utils import env, env_bool, env_list
from django.core.exceptions import ImproperlyConfigured

# ==============================================================================
# CORE & SECURITY
# ==============================================================================

# Ensure DEBUG is strictly disabled in production
DEBUG = False

# Enforce explicit SECRET_KEY configuration
if not SECRET_KEY or SECRET_KEY in PUBLISHED_SECRET_KEYS:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set in production to an unguessable string."
    )

# Enforce explicit ALLOWED_HOSTS configuration
if not ALLOWED_HOSTS or set(ALLOWED_HOSTS).issubset(LOCAL_HOSTS):
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS must be configured with production domain(s)."
    )

# Strict Cookie & Transport Security
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # Set to True unless client-side JavaScript reads the CSRF token

SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', default=True)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# HTTP Strict Transport Security (1 year + subdomains)
SECURE_HSTS_SECONDS = int(env('SECURE_HSTS_SECONDS', str(31536000)))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=True)
SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', default=True)

# Frame and content sniffing protections
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# ==============================================================================
# PERFORMANCE & DATABASE
# ==============================================================================

# Database persistent connections (reduces connection overhead under load)
DATABASES['default']['CONN_MAX_AGE'] = int(env('DATABASE_CONN_MAX_AGE', '60'))

# Sign-in and other limits in the database, so every worker shares the counts
# and a deploy does not reset them. The table is made by createcachetable.
CACHES['limits'] = {
    'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
    'LOCATION': 'marvin_limits',
    'TIMEOUT': CACHES['limits']['TIMEOUT'],
    'OPTIONS': {'MAX_ENTRIES': 100_000},
}

# For PostgreSQL set DATABASE_ENGINE=django.db.backends.postgresql and
# DATABASE_NAME, DATABASE_USER, DATABASE_PASSWORD, DATABASE_HOST, DATABASE_PORT.

# ==============================================================================
# STATIC & MEDIA ASSETS
# ==============================================================================

# Optimize static file serving via WhiteNoise manifest storage (gzip/brotli + long cache)
WHITENOISE_USE_FINDERS = False

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# ==============================================================================
# INTEGRATIONS & THIRD-PARTY SERVICES
# ==============================================================================

# Wise API Production defaults
WISE_API_SANDBOX = env_bool('WISE_API_SANDBOX', default=False)

# Two-Factor Authentication enforcement
TWO_FACTOR_ENFORCED = env_bool('TWO_FACTOR_ENFORCED', default=True)

# Live exchange rates fetching
EXCHANGE_RATES_FETCH = env_bool('EXCHANGE_RATES_FETCH', default=True)
WISE_BALANCE_FETCH = env_bool('WISE_BALANCE_FETCH', default=True)

# Email Delivery Configuration
MAILERS = {
    'default': {
        'BACKEND': "django.core.mail.backends.smtp.EmailBackend",
        'OPTIONS': {
            'host': env('EMAIL_HOST', 'localhost'),
            'use_tls': True,    # port is not needed: it defaults to 587 with use_tls True.
            'username': env('EMAIL_HOST_USER', ''),
            'password': env('EMAIL_HOST_PASSWORD', ''),
            'default_from_email': env('DEFAULT_FROM_EMAIL', ''),
        },
    },
}

# ==============================================================================
# LOGGING
# ==============================================================================

# Stream structured logs to stdout/stderr for container / host log collectors
LOGGING['handlers']['console'] = {
    'class': 'logging.StreamHandler',
    'formatter': 'wise',
}
LOGGING['root'] = {
    'handlers': ['console'],
    'level': env('LOG_LEVEL', 'INFO'),
}
