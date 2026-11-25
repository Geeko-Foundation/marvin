import base64
import os


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

