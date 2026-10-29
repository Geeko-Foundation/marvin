from unittest import mock

from django.test import TestCase
from django.urls import reverse

from profile.groups import GROUP_REQUESTER
from tsp.tests import make_user


class WiseRecipientTypeTests(TestCase):
    """Wise rejects an unknown account type outright, so the mapping matters."""

    def test_each_banking_method_maps_to_a_type_wise_accepts(self):
        from profile.models import BankingMethod
        from profile.services.wise_recipient import recipient_type_for_method

        # Confirmed against the sandbox's /v1/account-requirements:
        #   EUR -> iban, swift_code, email
        #   USD -> aba, fedwire_local, swift_code, email
        #   INR -> indian, indian_upi, email
        self.assertEqual(recipient_type_for_method(BankingMethod.IBAN), 'iban')
        self.assertEqual(recipient_type_for_method(BankingMethod.US_BANK), 'aba')
        self.assertEqual(recipient_type_for_method(BankingMethod.INDIA_IFSC), 'indian')

    def test_an_unknown_method_falls_back_to_iban(self):
        from profile.services.wise_recipient import recipient_type_for_method

        self.assertEqual(recipient_type_for_method(None), 'iban')
        self.assertEqual(recipient_type_for_method('something-else'), 'iban')


class WiseUsAccountTests(TestCase):
    """Wise refuses a US payee without an account type and a state code."""

    def _profile(self, state):
        from profile.models import UserProfile

        return UserProfile(state=state, country='US')

    def test_a_state_code_is_passed_through(self):
        from profile.services.wise_recipient import resolve_state

        self.assertEqual(resolve_state(self._profile('OR')), 'OR')
        self.assertEqual(resolve_state(self._profile('tx')), 'TX')

    def test_a_spelled_out_state_becomes_a_code(self):
        from profile.services.wise_recipient import resolve_state

        self.assertEqual(resolve_state(self._profile('Oregon')), 'OR')
        self.assertEqual(resolve_state(self._profile('new york')), 'NY')
        self.assertEqual(resolve_state(self._profile('District of Columbia')), 'DC')

    def test_anything_unrecognised_is_left_out(self):
        from profile.services.wise_recipient import resolve_state

        self.assertIsNone(resolve_state(self._profile('')))
        self.assertIsNone(resolve_state(self._profile('Bavaria')))
        self.assertIsNone(resolve_state(self._profile('XX')))

    def test_us_banking_sends_the_account_type(self):
        from profile.models import BankingMethod
        from profile.services.wise_recipient import bank_details_kwargs_from_banking

        kwargs = bank_details_kwargs_from_banking({
            'banking_method': BankingMethod.US_BANK,
            'bank_account_no': '12345678',
            'bank_routing_no': '111000025',
            'bank_account_type': 'CHECKING',
        })
        self.assertEqual(kwargs['us_account_type'], 'CHECKING')

    def test_the_form_asks_for_an_account_type(self):
        from profile.forms import WiseRecipientForm
        from profile.models import BankingMethod

        data = {
            'banking_method': BankingMethod.US_BANK,
            'banking_currency': 'USD',
            'bank_account_no': '12345678',
            'bank_routing_no': '111000025',
        }
        self.assertFalse(WiseRecipientForm(data=data).is_valid())
        self.assertTrue(WiseRecipientForm(data={**data, 'bank_account_type': 'SAVINGS'}).is_valid())

    def test_the_payee_payload_carries_both_fields(self):
        from payments.gateways.wise_api import WiseApiClient

        client = WiseApiClient(api_token='t', profile_id=1, sandbox=True)
        with mock.patch.object(client, '_request', return_value={'id': 1}) as request:
            client.create_payee(
                full_name='A Requester', email='a@example.org', currency='USD',
                account_type='aba', account_number='12345678', routing_number='111000025',
                us_account_type='CHECKING', country='US', address_line='1 Main Street',
                city='Portland', post_code='97201', state='OR',
            )
        details = request.call_args.args[2]['details']
        self.assertEqual(details['accountType'], 'CHECKING')
        self.assertEqual(details['address']['state'], 'OR')


class BankingFormRenderTests(TestCase):
    """Every method shares one account number input, so it must render once."""

    def setUp(self):
        self.user = make_user('rendertester', [GROUP_REQUESTER], onboarded=False)
        profile = self.user.profile
        profile.address_line, profile.city, profile.country = '1 Main St', 'Portland', 'US'
        profile.save()
        self.client.force_login(self.user)

    def test_no_field_is_rendered_twice(self):
        import re
        from collections import Counter

        html = self.client.get(reverse('signup_banking')).content.decode()
        names = re.findall(r'<(?:input|select)[^>]*name="([^"]+)"', html)
        repeated = [n for n, count in Counter(names).items()
                    if count > 1 and n != 'csrfmiddlewaretoken']
        # Two inputs sharing a name means the browser submits both and the empty
        # one wins, which silently broke US banking.
        self.assertEqual(repeated, [])

    def test_a_us_account_submitted_from_the_page_validates(self):
        from profile.forms import WiseRecipientForm
        from profile.models import BankingMethod

        form = WiseRecipientForm(data={
            'banking_method': BankingMethod.US_BANK,
            'banking_currency': 'USD',
            'bank_account_no': '12345678',
            'bank_routing_no': '111000025',
            'bank_account_type': 'CHECKING',
        })
        self.assertTrue(form.is_valid(), form.errors.as_text())
