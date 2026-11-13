"""Confirming a new account's email address before it can be used."""

from django.contrib.auth.tokens import PasswordResetTokenGenerator
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
