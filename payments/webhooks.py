"""Verification of Wise webhook deliveries."""

from __future__ import annotations

import base64
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings

logger = logging.getLogger('marvin.wise')

SIGNATURE_HEADER = 'HTTP_X_SIGNATURE_SHA256'
TEST_HEADER = 'HTTP_X_TEST_NOTIFICATION'
TRANSFER_EVENT = 'transfers#state-change'


def signing_key():
    """The configured Wise signing key, or None when there is none to use."""
    pem = (settings.WISE_WEBHOOK_PUBLIC_KEY or '').strip()
    if not pem:
        return None
    try:
        return serialization.load_pem_public_key(pem.encode())
    except (ValueError, TypeError):
        logger.error('WISE_WEBHOOK_PUBLIC_KEY is not a readable PEM public key')
        return None


def verify_signature(body: bytes, signature: str | None) -> bool:
    """
    Check Wise's RSA-SHA256 signature over the raw request body.

    Wise signs the bytes it sent, so the body must be the untouched request
    body rather than anything parsed and re-encoded.
    """
    key = signing_key()
    if key is None or not signature:
        return False
    try:
        key.verify(base64.b64decode(signature), body, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def transfer_id_from(event: dict) -> str | None:
    """
    The transfer a state-change event refers to, if it is one we act on.

    The event's own ``current_state`` is deliberately ignored. It names the
    transfer that changed and nothing more; what the transfer is now is read
    back from Wise.
    """
    if event.get('event_type') != TRANSFER_EVENT:
        return None
    resource = (event.get('data') or {}).get('resource') or {}
    transfer_id = resource.get('id')
    return str(transfer_id) if transfer_id else None
