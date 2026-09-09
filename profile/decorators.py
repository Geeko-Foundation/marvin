from profile.groups import GROUP_REQUESTER, assign_user_to_group
from profile.models import UserProfile


def get_profile(user):
    if not user.is_authenticated:
        return None
    profile, created = UserProfile.objects.get_or_create(user=user)
    if created:
        assign_user_to_group(user, GROUP_REQUESTER)
    return profile
