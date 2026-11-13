"""Confirming that an email address belongs to the person using it."""

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


class ActivationTokenGenerator(PasswordResetTokenGenerator):
    """
    A link that works once: it is tied to the account still being inactive.

    The separate salt keeps these links from ever working as password reset
    links, and the other way round.
    """

    key_salt = 'marvin.accounts.ActivationTokenGenerator'

    def _make_hash_value(self, user, timestamp):
        return f'{user.pk}{user.is_active}{user.password}{timestamp}'


activation_token = ActivationTokenGenerator()


def send_activation_email(request, user):
    """Email ``user`` the link that switches their account on."""
    link = request.build_absolute_uri(reverse('signup_activate', kwargs={
        'uidb64': urlsafe_base64_encode(force_bytes(user.pk)),
        'token': activation_token.make_token(user),
    }))
    send_mail(
        'Confirm your email address for Marvin',
        render_to_string('email/activate_account.txt', {'user': user, 'link': link}),
        None,
        [user.email],
    )


EMAIL_CHANGE_SALT = 'marvin.accounts.email_change'
EMAIL_CHANGE_MAX_AGE = 3 * 24 * 60 * 60


def send_email_change_link(request, user, new_email):
    """
    Email ``new_email`` a link that moves the account over to it.

    The link names the old address too, so it stops working once the change
    has been made or the address has changed some other way.
    """
    token = signing.dumps({'user': user.pk, 'from': user.email, 'to': new_email}, salt=EMAIL_CHANGE_SALT)
    link = request.build_absolute_uri(reverse('email_change_confirm', kwargs={'token': token}))
    send_mail(
        'Confirm your new email address for Marvin',
        render_to_string('email/confirm_email_change.txt', {'user': user, 'link': link}),
        None,
        [new_email],
    )


def read_email_change_link(token):
    """Return ``(user_pk, old_email, new_email)`` from a link, or ``None`` if it is bad or old."""
    try:
        data = signing.loads(token, salt=EMAIL_CHANGE_SALT, max_age=EMAIL_CHANGE_MAX_AGE)
    except signing.BadSignature:
        return None
    return data['user'], data['from'], data['to']
