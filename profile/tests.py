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
