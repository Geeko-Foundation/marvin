from django.contrib.auth.models import Group, User
from django.test import TestCase

from profile.decorators import get_profile
from profile.groups import GROUP_PERMISSIONS, GROUP_REQUESTER, user_in_group


class GroupSetupTests(TestCase):
    # The test database is built from every migration, so these only run at all
    # if a fresh database can migrate.

    def test_groups_have_their_permissions(self):
        for name, codenames in GROUP_PERMISSIONS.items():
            group = Group.objects.get(name=name)
            self.assertEqual(
                set(group.permissions.values_list('codename', flat=True)),
                set(codenames),
            )


class GetProfileTests(TestCase):

    def test_new_profile_joins_requester_group(self):
        user = User.objects.create_user('newbie', password='x')
        get_profile(user)
        self.assertTrue(user_in_group(user, GROUP_REQUESTER))

    def test_existing_profile_is_reused(self):
        user = User.objects.create_user('again', password='x')
        self.assertEqual(get_profile(user).pk, get_profile(user).pk)


class PushDeliveryTests(TestCase):
    """Alerts reach anyone who subscribed a browser, signed with a key pywebpush can read."""

    def test_the_configured_private_key_loads_and_matches_the_public_key(self):
        import base64

        from cryptography.hazmat.primitives import serialization
        from django.conf import settings
        from py_vapid import Vapid

        keys = settings.WEBPUSH_SETTINGS
        vapid = Vapid.from_string(keys['VAPID_PRIVATE_KEY'])
        public = vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.assertEqual(base64.urlsafe_b64encode(public).decode().rstrip('='), keys['VAPID_PUBLIC_KEY'])

    def test_a_pem_key_is_converted(self):
        from marvin.settings import _DEV_VAPID_PRIVATE_KEY, vapid_private_key
        converted = vapid_private_key(_DEV_VAPID_PRIVATE_KEY.replace('\n', '\\n'))
        self.assertNotIn('BEGIN', converted)
        self.assertEqual(vapid_private_key(converted), converted)

    def test_a_subscribed_user_is_sent_an_alert(self):
        from unittest import mock

        from django.contrib.auth.models import User
        from webpush.models import PushInformation, SubscriptionInfo

        from profile.services.push_notifications import send_push_to_user

        user = User.objects.create_user('pushee', password='x')
        with mock.patch('webpush.send_user_notification') as send:
            send_push_to_user(user, head='Hi', body='There')
            send.assert_not_called()
            sub = SubscriptionInfo.objects.create(browser='firefox', endpoint='https://push.example/1',
                                                  auth='a', p256dh='p')
            PushInformation.objects.create(user=user, subscription=sub)
            send_push_to_user(user, head='Hi', body='There')
            send.assert_called_once()
