from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from profile.groups import GROUP_FINANCE, assign_user_to_group
from profile.models import UserProfile
from tsp.models import RequestState
from tsp.test_advances import make_request
from tsp.tests import make_user


@override_settings(USE_WISE=False)
class WithoutWiseTests(TestCase):
    """With USE_WISE off, nobody is asked for bank details and the Wise pages are gone."""

    def setUp(self):
        self.user = User.objects.create_user('nowise', password='x', email='nowise@example.org', first_name='No')
        UserProfile.objects.create(user=self.user, address_line='1 Main St', city='Pune',
                                   country='India', postal_code='411001')
        assign_user_to_group(self.user, 'Requester')
        self.client.force_login(self.user)

    def test_a_requester_without_a_wise_account_is_not_sent_to_banking(self):
        self.assertFalse(self.user.profile.requires_wise_banking)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_the_wizard_ends_after_the_profile_step(self):
        response = self.client.get(reverse('signup_profile'))
        self.assertRedirects(response, reverse('signup_complete'))
        self.assertNotContains(self.client.get(reverse('signup_complete')), 'Wise')

    def test_wise_pages_are_not_found(self):
        for name in ('signup_banking', 'signup_banking_progress', 'onboarding_wise'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 404, name)
        for name in ('signup_banking_step', 'signup_skip_banking', 'check_wise_account'):
            self.assertEqual(self.client.post(reverse(name)).status_code, 404, name)
        self.assertEqual(self.client.post(reverse('payments:wise_webhook')).status_code, 404)

    def test_the_request_form_has_no_wise_account_check(self):
        self.assertNotContains(self.client.get(reverse('request_create')), 'Payment Account')

    def test_finance_can_pay_someone_without_a_wise_account(self):
        make_request(self.user, RequestState.SUBMITTED_FOR_PAYMENT)
        self.client.force_login(make_user('nowisefinance', [GROUP_FINANCE]))
        response = self.client.get(reverse('payments:dashboard'))
        self.assertContains(response, 'Process Payment')
        self.assertNotContains(response, 'must finish setting up Wise')
        self.assertNotContains(response, 'Wise not set up')


class WithWiseTests(TestCase):
    def test_on_by_default(self):
        user = User.objects.create_user('withwise', password='x')
        UserProfile.objects.create(user=user)
        assign_user_to_group(user, 'Requester')
        self.assertTrue(user.profile.requires_wise_banking)
