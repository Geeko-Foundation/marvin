from django.test import TestCase, override_settings
from django.urls import reverse

from tsp.models import TravelRequest
from tsp.tests import make_committee, make_event, make_requester


@override_settings(
    BRAND_APP_NAME='Wanderer',
    BRAND_ORG_NAME='GNOME Foundation',
    BRAND_TAGLINE='Conference Travel Subsidy',
    BRAND_THEME_LIGHT='emerald',
    BRAND_THEME_DARK='forest',
)
class BrandingTests(TestCase):
    """A fork should be able to rename and recolour everything from settings."""

    def test_the_login_page_uses_the_configured_names(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, 'Wanderer')
        self.assertContains(response, 'GNOME Foundation')
        self.assertNotContains(response, 'Marvin')
        self.assertNotContains(response, 'Geeko')

    def test_the_signed_in_pages_use_them_too(self):
        self.client.force_login(make_committee('brandcommittee'))
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'Wanderer')
        self.assertContains(response, 'Conference Travel Subsidy')
        self.assertNotContains(response, 'Marvin')

    def test_the_signup_wizard_and_printable_form_use_them(self):
        response = self.client.get(reverse('signup'))
        self.assertContains(response, 'Conference Travel Subsidy')
        self.assertNotContains(response, 'Travel Support Program')

        owner = make_requester('brandowner')
        travel_request = TravelRequest.objects.create(
            user=owner, event=make_event(), description='Talk',
        )
        self.client.force_login(owner)
        form = self.client.get(reverse('request_form', args=[travel_request.pk]))
        self.assertContains(form, 'Conference Travel Subsidy')
        self.assertNotContains(form, 'Travel Support Program')

    def test_the_themes_are_configurable(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, 'data-theme="emerald"')
        self.assertContains(response, "var DARK = 'forest'")

    def test_the_defaults_are_still_marvin(self):
        with self.settings(BRAND_APP_NAME='Marvin', BRAND_ORG_NAME='Geeko Foundation'):
            response = self.client.get(reverse('login'))
            self.assertContains(response, 'Marvin')
            self.assertContains(response, 'Geeko Foundation')
