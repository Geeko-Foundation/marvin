import base64
import os
from urllib.parse import unquote, urlsplit


def env(key: str, default: str = '') -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    return value.lower() in ('1', 'true', 'yes')


def env_list(key: str, default: str = '') -> list[str]:
    raw = env(key, default)
    return [item.strip() for item in raw.split(',') if item.strip()]


def normalize_vapid_private_key(value: str) -> str:
    """
    Convert a PEM private key to URL-safe base64 DER bytes format expected by pywebpush.
    If already in raw format, return as is.
    """
    if not value:
        return value
    value = value.replace('\\n', '\n').strip()
    if '-----BEGIN' not in value:
        return value
    body = ''.join(line for line in value.splitlines() if not line.startswith('-----'))
    return base64.urlsafe_b64encode(base64.b64decode(body)).decode().rstrip('=')


# Alias for backwards compatibility
vapid_private_key = normalize_vapid_private_key


def parse_database_url(url: str) -> dict[str, str]:
    """
    Parse a database connection URL (e.g. postgresql://user:password@host:port/name).
    """
    if not url:
        return {}
    parsed = urlsplit(url)
    engine = 'django.db.backends.postgresql'
    if parsed.scheme in ('postgres', 'postgresql'):
        engine = 'django.db.backends.postgresql'
    elif parsed.scheme == 'sqlite':
        return {'ENGINE': 'django.db.backends.sqlite3', 'NAME': unquote(parsed.path)}

    return {
        'ENGINE': engine,
        'NAME': unquote(parsed.path.lstrip('/')),
        'USER': unquote(parsed.username or ''),
        'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname or '',
        'PORT': str(parsed.port or ''),
    }
