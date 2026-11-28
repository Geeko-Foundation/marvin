import re

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from profile.models import UserProfile
from profile.groups import GROUP_FINANCE, GROUP_REQUESTER, GROUP_TSP
from tsp.tests import make_user

PASSWORD = 'a-Long-pass-phrase-9'


def link_in(message):
    return re.search(r'https?://testserver(/\S+)', message.body).group(1)


class EmailConfirmationTests(TestCase):
    """A new account works only once its email address has been confirmed."""

    def sign_up(self, email='newcomer@example.org'):
        return self.client.post(reverse('signup'), {
            'email': email, 'first_name': 'New', 'last_name': 'Comer',
            'password1': PASSWORD, 'password2': PASSWORD,
        })

    def test_signing_up_sends_a_link_and_the_account_waits_for_it(self):
        response = self.sign_up()
        self.assertContains(response, 'Confirm your email')
        self.assertFalse(User.objects.get(username='newcomer@example.org').is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(self.client.login(username='newcomer@example.org', password=PASSWORD))

    def test_the_link_switches_the_account_on_once(self):
        self.sign_up()
        link = link_in(mail.outbox[0])
        self.assertRedirects(self.client.get(link), reverse('login'), fetch_redirect_response=False)
        self.assertTrue(User.objects.get(username='newcomer@example.org').is_active)
        self.assertTrue(self.client.login(username='newcomer@example.org', password=PASSWORD))

        self.client.logout()
        self.client.get(link)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_link_can_be_sent_again_and_unknown_addresses_look_the_same(self):
        self.sign_up()
        self.client.post(reverse('signup_resend'), {'email': 'newcomer@example.org'})
        self.assertEqual(len(mail.outbox), 2)
        response = self.client.post(reverse('signup_resend'), {'email': 'nobody@example.org'})
        self.assertContains(response, 'Confirm your email')
        self.assertEqual(len(mail.outbox), 2)

    def test_a_password_reset_link_does_not_confirm_an_account(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        self.sign_up()
        user = User.objects.get(username='newcomer@example.org')
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        self.client.get(reverse('signup_activate', args=[uid, default_token_generator.make_token(user)]))
        user.refresh_from_db()
        self.assertFalse(user.is_active)


class PasswordChangeTests(TestCase):
    def test_changing_the_password_goes_through_an_emailed_link(self):
        user = make_user('changer', [GROUP_REQUESTER])
        self.client.force_login(user)
        self.client.post(reverse('password_change_email'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertIn('/password/reset/', link_in(mail.outbox[0]))


@override_settings(TWO_FACTOR_ENFORCED=True)
class TwoStepSignInTests(TestCase):
    """Staff need a code from their phone as well as their password."""

    def code(self, device):
        return str(totp(device.bin_key, device.step, device.t0, device.digits, device.drift)).zfill(device.digits)

    def set_up_two_step(self, user):
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse('dashboard')),
                             reverse('two_factor_setup') + '?next=%2F', fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('two_factor_setup')), '<svg')
        device = TOTPDevice.objects.get(user=user)
        response = self.client.post(reverse('two_factor_setup'), {'token': self.code(device)})
        self.assertContains(response, 'Save your backup codes')
        return re.findall(r'<li class="bg-base-200 rounded px-2 py-1">(\w+)</li>', response.content.decode())

    def test_finance_has_to_set_it_up_before_using_marvin(self):
        finance = make_user('otpfinance', [GROUP_FINANCE])
        codes = self.set_up_two_step(finance)
        self.assertEqual(len(codes), 10)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_a_new_superuser_reaches_two_step_setup_instead_of_looping(self):
        # createsuperuser makes no profile, so the first visit files them under
        # Requester with onboarding to do; the two redirects used to chase each other.
        admin = User.objects.create_superuser('firstadmin', 'admin@example.org', 'x')
        self.client.force_login(admin)
        self.assertRedirects(self.client.get(reverse('dashboard')),
                             reverse('two_factor_setup') + '?next=%2F', fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('two_factor_setup')), '<svg')

    def test_the_next_sign_in_asks_for_a_code_and_a_backup_code_works_once(self):
        committee = make_user('otpcommittee', [GROUP_TSP])
        codes = self.set_up_two_step(committee)
        self.client.logout()
        self.client.force_login(committee)
        self.assertRedirects(self.client.get(reverse('dashboard')),
                             reverse('two_factor_verify') + '?next=%2F', fetch_redirect_response=False)
        self.assertRedirects(self.client.post(reverse('two_factor_verify'), {'token': codes[0]}),
                             reverse('dashboard'), fetch_redirect_response=False)
        self.client.logout()
        self.client.force_login(committee)
        self.assertContains(self.client.post(reverse('two_factor_verify'), {'token': codes[0]}), 'did not work')

    def test_a_code_from_the_app_signs_in(self):
        committee = make_user('otpapp', [GROUP_TSP])
        device = TOTPDevice.objects.create(user=committee, name='Authenticator app', confirmed=True)
        self.client.force_login(committee)
        self.assertRedirects(self.client.post(reverse('two_factor_verify'), {'token': self.code(device)}),
                             reverse('dashboard'), fetch_redirect_response=False)

    def test_a_password_alone_cannot_link_a_second_app(self):
        committee = make_user('otpthief', [GROUP_TSP])
        TOTPDevice.objects.create(user=committee, name='Authenticator app', confirmed=True)
        self.client.force_login(committee)
        self.assertRedirects(self.client.get(reverse('two_factor_setup')), reverse('two_factor_verify'),
                             fetch_redirect_response=False)
        self.assertEqual(TOTPDevice.objects.filter(user=committee).count(), 1)

    def test_requesters_choose_for_themselves_and_staff_cannot_turn_it_off(self):
        requester = make_user('otprequester', [GROUP_REQUESTER])
        self.client.force_login(requester)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        self.set_up_two_step_optional(requester)
        self.client.post(reverse('two_factor_disable'))
        self.assertFalse(TOTPDevice.objects.filter(user=requester).exists())
        self.assertFalse(StaticDevice.objects.filter(user=requester).exists())

        finance = make_user('otpkeeps', [GROUP_FINANCE])
        self.set_up_two_step(finance)
        self.client.post(reverse('two_factor_disable'))
        self.assertTrue(TOTPDevice.objects.filter(user=finance).exists())

    def set_up_two_step_optional(self, user):
        self.client.get(reverse('two_factor_setup'))
        device = TOTPDevice.objects.get(user=user)
        self.client.post(reverse('two_factor_setup'), {'token': self.code(device)})
        self.assertTrue(TOTPDevice.objects.get(user=user).confirmed)


class IncompleteAddressTests(TestCase):
    """An account with Wise set up but its country missing can finish it."""

    def test_the_missing_field_is_asked_for_instead_of_looping(self):
        user = make_user('nocountry', [GROUP_REQUESTER])
        user.profile.country = ''
        user.profile.save()
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse('dashboard')), reverse('signup_profile'),
                             fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('signup_profile')).status_code, 200)
        self.assertRedirects(self.client.get(reverse('signup_complete')), reverse('signup_profile'),
                             fetch_redirect_response=False)

        user.profile.country = 'India'
        user.profile.save()
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)


class EmailChangeTests(TestCase):
    """A new email address is used only once a link sent to it is opened."""

    def setUp(self):
        self.user = make_user('mover', [GROUP_TSP])
        self.user.email = 'old@example.org'
        self.user.save()
        self.client.force_login(self.user)

    def change_to(self, email, password='x'):
        return self.client.post(reverse('profile'), {
            'first_name': 'Mo', 'last_name': 'Ver', 'email': email, 'current_password': password,
        })

    def test_a_new_address_needs_the_current_password(self):
        response = self.change_to('new@example.org', password='wrong')
        self.assertContains(response, 'Enter your current password to change your email.')
        self.assertEqual(len(mail.outbox), 0)

    def test_the_old_address_is_told_once_the_change_is_made(self):
        self.change_to('new@example.org')
        self.client.get(link_in(mail.outbox[-1]))
        self.assertEqual(mail.outbox[-1].to, ['old@example.org'])


    def test_the_new_address_waits_for_its_link(self):
        self.change_to('new@example.org')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.org')
        self.assertEqual(mail.outbox[-1].to, ['new@example.org'])
        self.client.get(link_in(mail.outbox[-1]))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@example.org')

    def test_the_link_works_once(self):
        self.change_to('new@example.org')
        link = link_in(mail.outbox[-1])
        self.client.get(link)
        self.user.email = 'third@example.org'
        self.user.save()
        self.client.get(link)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'third@example.org')

    def test_an_address_another_account_uses_is_refused(self):
        make_user('other', [GROUP_TSP]).__class__.objects.filter(username='other').update(email='taken@example.org')
        response = self.change_to('taken@example.org')
        self.assertContains(response, 'An account with this email already exists.')
        self.assertEqual(len(mail.outbox), 0)

    def test_keeping_the_same_address_sends_nothing(self):
        self.change_to('OLD@example.org')
        self.assertEqual(len(mail.outbox), 0)


class EmailAsUsernameTests(TestCase):
    """New accounts sign in with their email, so every username is unique by email."""

    def sign_up(self, email):
        return self.client.post(reverse('signup'), {
            'first_name': 'Ada', 'last_name': 'Lovelace', 'email': email,
            'password1': 'a-long-unusual-passphrase', 'password2': 'a-long-unusual-passphrase',
        })

    def test_the_email_becomes_the_username(self):
        self.sign_up('Ada@Example.org')
        self.assertTrue(User.objects.filter(username='ada@example.org', email='ada@example.org').exists())

    def test_an_email_in_use_cannot_sign_up_again(self):
        self.sign_up('ada@example.org')
        self.assertContains(self.sign_up('ADA@example.org'), 'An account with this email already exists.')
        self.assertEqual(User.objects.filter(email__iexact='ada@example.org').count(), 1)

    def test_an_older_account_signs_in_with_its_email_or_username(self):
        user = make_user('oldname', [GROUP_REQUESTER])
        for name in ('oldname', 'OLDNAME@example.org'):
            with self.subTest(name=name):
                self.client.logout()
                self.client.post(reverse('login'), {'username': name, 'password': 'x'})
                self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_changing_the_email_moves_the_username_with_it(self):
        user = User.objects.create_user('ada@example.org', 'ada@example.org', 'x')
        UserProfile.objects.create(user=user, country='India')
        self.client.force_login(user)
        self.client.post(reverse('profile'), {
            'first_name': 'Ada', 'last_name': 'L', 'email': 'new@example.org', 'current_password': 'x',
        })
        self.client.get(link_in(mail.outbox[-1]))
        user.refresh_from_db()
        self.assertEqual((user.username, user.email), ('new@example.org', 'new@example.org'))
