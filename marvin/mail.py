"""Sending the site's emails: one place for the branding, the links and what happens when mail fails."""

import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

logger = logging.getLogger('marvin.email')


def absolute_url(path: str) -> str:
    """A full link for an email, built from SITE_BASE_URL since there may be no request."""
    return settings.SITE_BASE_URL.rstrip('/') + path


def send_email(to: str, subject: str, template: str, context: dict) -> bool:
    """
    Render ``template`` and send it to one address. Returns whether it went.

    A mail server being down never breaks the page or the change that sent
    the email; the failure is logged and the caller decides what to tell the user.
    """
    if not to:
        return False
    context = {
        'app_name': settings.BRAND_APP_NAME,
        'org_name': settings.BRAND_ORG_NAME,
        'site_url': settings.SITE_BASE_URL.rstrip('/'),
        **context,
    }
    try:
        body = render_to_string(template, context)
        EmailMessage(subject=subject, body=body, to=[to]).send()
    except Exception:
        logger.exception('Email "%s" to %s was not sent', subject, to)
        return False
    return True
