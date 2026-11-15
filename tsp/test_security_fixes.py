"""Regression tests for the problems the November 2026 security review found."""

import re
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import caches
from django.db.models import ProtectedError
from django.test import TestCase, override_settings

from django.urls import reverse

from profile.groups import GROUP_FINANCE, GROUP_REQUESTER
from tsp.models import RequestState, TravelRequest
from tsp.test_advances import make_request
from tsp.tests import make_user


PASSWORD = 'a-Long-pass-phrase-9'


def link_in(message):
    return re.search(r'https?://testserver(/\S+)', message.body).group(1)


class AccountTests(TestCase):

    def setUp(self):
        caches['limits'].clear()

    def sign_up(self, email='newcomer@example.org', password=PASSWORD):
        return self.client.post(reverse('signup'), {
            'username': 'newcomer', 'email': email, 'first_name': 'New', 'last_name': 'Comer',
            'password1': password, 'password2': password,
        })

    def test_a_switched_off_account_cannot_switch_itself_back_on(self):
        user = make_user('leaver', [GROUP_FINANCE])
        self.client.force_login(user)
        self.client.logout()
        User.objects.filter(pk=user.pk).update(is_active=False)

        self.client.post(reverse('signup_resend'), {'email': user.email})
        self.assertEqual(len(mail.outbox), 0)

    def test_the_link_confirms_the_address_without_signing_in(self):
        self.sign_up()
        response = self.client.get(link_in(mail.outbox[0]))
        self.assertRedirects(response, reverse('login'), fetch_redirect_response=False)
        self.assertTrue(User.objects.get(username='newcomer').is_active)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_sign_up_refuses_a_weak_password(self):
        response = self.sign_up(password='a')
        self.assertContains(response, 'This password is too short.')
        self.assertFalse(User.objects.filter(username='newcomer').exists())

    def test_the_link_can_be_sent_again_only_a_few_times_a_day(self):
        self.sign_up()
        for _ in range(5):
            self.client.post(reverse('signup_resend'), {'email': 'newcomer@example.org'})
        self.assertEqual(len(mail.outbox), 1 + 3)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SignInLimitTests(TestCase):

    def setUp(self):
        caches['limits'].clear()
        self.user = make_user('target', [GROUP_REQUESTER])
        self.user.set_password(PASSWORD)
        self.user.save()

    def test_made_up_names_cannot_push_a_lock_out(self):
        for _ in range(10):
            self.client.post(reverse('login'), {'username': 'target', 'password': 'wrong'})
        # More than the default cache holds, which used to flush the count.
        for i in range(400):
            self.client.post(reverse('login'), {'username': f'nobody{i}', 'password': 'x'})
        response = self.client.post(reverse('login'), {'username': 'target', 'password': PASSWORD})
        self.assertContains(response, 'Too many failed sign-in attempts')

    def test_the_admin_signs_in_through_the_same_page(self):
        response = self.client.get('/admin/login/?next=/admin/')
        self.assertRedirects(response, reverse('login') + '?next=/admin/', fetch_redirect_response=False)


class NewRequestAccessTests(TestCase):

    def test_a_visitor_is_sent_to_sign_in(self):
        response = self.client.get(reverse('request_create'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_finance_alone_cannot_create_a_request(self):
        self.client.force_login(make_user('counter', [GROUP_FINANCE]))
        self.assertEqual(self.client.get(reverse('request_create')).status_code, 403)


class FinanceOwnRequestTests(TestCase):

    def test_finance_cannot_settle_their_own_request(self):
        member = make_user('payer', [GROUP_FINANCE, GROUP_REQUESTER])
        travel_request = make_request(member, RequestState.SUBMITTED_FOR_PAYMENT)
        self.client.force_login(member)
        with mock.patch.object(TravelRequest, 'advance_covers_costs', return_value=True):
            self.client.post(reverse('request_settle', args=[travel_request.pk]), {'action': 'close'})
        travel_request.refresh_from_db()
        self.assertEqual(travel_request.state, RequestState.SUBMITTED_FOR_PAYMENT)

    def test_finance_cannot_pay_their_own_request(self):
        from payments.services import PaymentService
        member = make_user('payer', [GROUP_FINANCE, GROUP_REQUESTER])
        travel_request = make_request(member, RequestState.SUBMITTED_FOR_PAYMENT)
        with self.assertRaisesMessage(ValueError, 'You cannot pay your own request'):
            PaymentService().process_payment(travel_request.pk, 'manual', member)


class RecordKeepingTests(TestCase):

    def test_someone_with_requests_cannot_be_deleted(self):
        owner = make_user('owner', [GROUP_REQUESTER])
        make_request(owner, RequestState.PAID)
        with self.assertRaises(ProtectedError):
            owner.delete()


class LiveBankCheckTests(TestCase):

    def test_a_live_account_sends_no_test_transfer(self):
        from profile.services.wise_verification import run_verification_step
        profile = make_user('payee', [GROUP_REQUESTER]).profile
        state = {'account_id': 5, 'banking': {'banking_method': 'iban', 'bank_iban': 'DE89370400440532013000'}}
        client = mock.Mock(sandbox=False)
        with mock.patch('profile.services.wise_verification.get_wise_client', return_value=client):
            for step in ('create_quote', 'create_test_transfer', 'fund_transfer', 'reverse_transfer'):
                self.assertIn('skipped', run_verification_step(profile, state, step))
        client.create_transfer.assert_not_called()
        client.fund_or_advance_transfer.assert_not_called()

    def test_bank_setup_stops_after_a_few_tries_a_day(self):
        caches['limits'].clear()
        user = make_user('retrier', [GROUP_REQUESTER], onboarded=False)
        profile = user.profile
        profile.address_line, profile.city, profile.country, profile.postal_code = '1 St', 'Berlin', 'DE', '10115'
        profile.save()
        self.client.force_login(user)
        data = {'banking_method': 'iban', 'banking_currency': 'EUR', 'bank_iban': 'DE89370400440532013000'}
        for _ in range(5):
            self.client.post(reverse('signup_banking'), data)
        response = self.client.post(reverse('signup_banking'), data)
        self.assertContains(response, 'Too many attempts to set up bank details today.')


class CsvSourceCurrencyTests(TestCase):

    def test_the_batch_file_uses_the_configured_source_currency(self):
        from payments.base import PaymentRequest
        from payments.gateways.wise import WiseCsvGateway
        request = PaymentRequest(amount=Decimal('10'), currency='GBP', recipient_name='A',
                                 recipient_email='a@example.org', reference='TSP1', metadata={})
        with self.settings(WISE_SOURCE_CURRENCY='GBP'):
            content = WiseCsvGateway().export_batch([request]).content
        self.assertIn('TSP1,PERSON,GBP,10,GBP,GBP', content)
