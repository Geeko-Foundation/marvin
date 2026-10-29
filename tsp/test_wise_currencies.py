from unittest import mock

from django.test import TestCase
from django.urls import reverse

from profile.groups import GROUP_REQUESTER
from tsp.tests import make_user


class WiseCountryCoverageTests(TestCase):
    """Marvin offers ten currencies, and Wise takes a different rail for each."""

    def test_every_offered_currency_has_a_method(self):
        from profile.forms import CURRENCY_CHOICES
        from profile.services.wise_recipient import METHOD_FOR_CURRENCY

        # EUR, GBP, CZK and PLN go over IBAN; the rest need their own rail, and
        # Wise answers "Invalid currency for this account type" without it.
        iban_currencies = {'EUR', 'GBP', 'CZK', 'PLN'}
        for currency, _ in CURRENCY_CHOICES:
            with self.subTest(currency=currency):
                self.assertTrue(currency in iban_currencies
                                or currency in METHOD_FOR_CURRENCY)

    def test_each_method_maps_to_the_type_wise_names(self):
        from profile.models import BankingMethod
        from profile.services.wise_recipient import recipient_type_for_method

        # Confirmed against the sandbox: all ten corridors return CREATED.
        self.assertEqual(recipient_type_for_method(BankingMethod.AUSTRALIA), 'australian')
        self.assertEqual(recipient_type_for_method(BankingMethod.BRAZIL), 'brazil')
        self.assertEqual(recipient_type_for_method(BankingMethod.INDONESIA), 'indonesian')
        self.assertEqual(recipient_type_for_method(BankingMethod.JAPAN), 'japanese')

    def test_a_method_and_currency_that_disagree_are_rejected(self):
        from profile.forms import WiseRecipientForm
        from profile.models import BankingMethod

        form = WiseRecipientForm(data={
            'banking_method': BankingMethod.JAPAN, 'banking_currency': 'EUR',
            'bank_account_no': '1234567', 'bank_code': '0443',
            'bank_japan_branch': '001', 'bank_account_type': 'CURRENT',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('paid in JPY', form.errors.as_text())

    def test_an_iban_cannot_be_used_for_a_currency_that_needs_its_own_rail(self):
        from profile.forms import WiseRecipientForm
        from profile.models import BankingMethod

        form = WiseRecipientForm(data={
            'banking_method': BankingMethod.IBAN, 'banking_currency': 'BRL',
            'bank_iban': 'DE89370400440532013000',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('Brazil', form.errors.as_text())

    def test_each_method_asks_for_the_fields_wise_requires(self):
        from profile.forms import WiseRecipientForm
        from profile.models import BankingMethod

        form = WiseRecipientForm(data={
            'banking_method': BankingMethod.BRAZIL, 'banking_currency': 'BRL',
            'bank_account_no': '12345678-9',
        })
        self.assertFalse(form.is_valid())
        errors = form.errors.as_text()
        for expected in ('bank', 'branch code', 'CPF', 'account type'):
            self.assertIn(expected, errors)

    def test_an_account_type_from_another_country_is_rejected(self):
        from profile.forms import WiseRecipientForm
        from profile.models import BankingMethod

        # PAYMENT is a Brazilian value; Japan takes current, savings or checking.
        form = WiseRecipientForm(data={
            'banking_method': BankingMethod.JAPAN, 'banking_currency': 'JPY',
            'bank_account_no': '1234567', 'bank_code': '0443',
            'bank_japan_branch': '001', 'bank_account_type': 'PAYMENT',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('current, savings or checking', form.errors.as_text())

    def test_the_new_methods_send_the_right_details(self):
        from profile.models import BankingMethod
        from profile.services.wise_recipient import bank_details_kwargs_from_banking

        australia = bank_details_kwargs_from_banking({
            'banking_method': BankingMethod.AUSTRALIA,
            'bank_account_no': '123456789', 'bank_bsb': '802985'})
        self.assertEqual(australia['bsb_code'], '802985')

        japan = bank_details_kwargs_from_banking({
            'banking_method': BankingMethod.JAPAN, 'bank_account_no': '1234567',
            'bank_code': '0443', 'bank_japan_branch': '001',
            'bank_account_type': 'CURRENT'})
        self.assertEqual(japan['branch_code'], '001')
        self.assertEqual(japan['bank_code'], '0443')

        brazil = bank_details_kwargs_from_banking({
            'banking_method': BankingMethod.BRAZIL, 'bank_account_no': '12345678-9',
            'bank_code': '1', 'bank_branch': '1234', 'bank_cpf': '52998224725',
            'bank_account_type': 'CHECKING'})
        self.assertEqual(brazil['cpf'], '52998224725')


class WiseAddressTests(TestCase):
    """Wise wants a post code everywhere and a state for two countries."""

    def test_a_spelled_out_country_becomes_a_code(self):
        from profile.models import UserProfile
        from profile.services.wise_recipient import resolve_country

        # Wise answers "Must be ISO 3166-1 alpha-2 country code" for free text.
        self.assertEqual(resolve_country(UserProfile(country='United States'), {}), 'US')
        self.assertEqual(resolve_country(UserProfile(country='japan'), {}), 'JP')
        self.assertEqual(resolve_country(UserProfile(country='AU'), {}), 'AU')

    def test_an_australian_state_resolves(self):
        from profile.models import UserProfile
        from profile.services.wise_recipient import resolve_state

        profile = UserProfile(country='Australia', state='Victoria')
        self.assertEqual(resolve_state(profile, 'AU'), 'VIC')

    def test_wa_depends_on_the_country(self):
        from profile.models import UserProfile
        from profile.services.wise_recipient import resolve_state

        # Washington in one list, Western Australia in the other.
        self.assertEqual(resolve_state(UserProfile(country='US', state='WA'), 'US'), 'WA')
        self.assertEqual(resolve_state(UserProfile(country='AU', state='WA'), 'AU'), 'WA')
        self.assertIsNone(resolve_state(UserProfile(country='DE', state='WA'), 'DE'))

    def test_a_profile_without_a_post_code_is_incomplete(self):
        user = make_user('nopostcode', [GROUP_REQUESTER], onboarded=False)
        profile = user.profile
        profile.address_line, profile.city, profile.country = '1 Main St', 'Berlin', 'DE'
        profile.save()
        self.assertFalse(profile.is_profile_complete())
        profile.postal_code = '10115'
        profile.save()
        self.assertTrue(profile.is_profile_complete())

    def test_a_us_profile_without_a_state_is_incomplete(self):
        user = make_user('nostate', [GROUP_REQUESTER], onboarded=False)
        profile = user.profile
        profile.address_line, profile.city = '1 Main St', 'Portland'
        profile.country, profile.postal_code = 'US', '97201'
        profile.save()
        self.assertFalse(profile.is_profile_complete())
        profile.state = 'Oregon'
        profile.save()
        self.assertTrue(profile.is_profile_complete())


class BankingOptionsEndpointTests(TestCase):
    """The page fills the bank and branch selects from Wise, not from a fixture."""

    def setUp(self):
        self.user = make_user('optionsuser', [GROUP_REQUESTER])
        self.client.force_login(self.user)

    def test_it_returns_the_banks_for_a_method(self):
        with mock.patch('tsp.views.bank_choices', return_value=[('0443', 'BNP Paribas')]) as banks:
            response = self.client.get(reverse('signup_banking_options'), {'method': 'japan'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['options'], [{'value': '0443', 'label': 'BNP Paribas'}])
        banks.assert_called_once_with('japan')

    def test_a_chosen_bank_asks_for_its_branches(self):
        with mock.patch('tsp.views.branch_choices', return_value=[('001', 'Tokyo')]) as branches:
            response = self.client.get(reverse('signup_banking_options'),
                                       {'method': 'japan', 'bank_code': '0443'})
        self.assertEqual(response.json()['options'], [{'value': '001', 'label': 'Tokyo'}])
        branches.assert_called_once_with('japan', '0443')

    def test_onboarding_does_not_redirect_the_lookup(self):
        # The page calls this mid signup, so the middleware has to let it through.
        incomplete = make_user('midsignup', [GROUP_REQUESTER], onboarded=False)
        self.client.force_login(incomplete)
        with mock.patch('tsp.views.bank_choices', return_value=[]):
            response = self.client.get(reverse('signup_banking_options'), {'method': 'brazil'})
        self.assertEqual(response.status_code, 200)
