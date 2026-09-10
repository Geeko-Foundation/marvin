from profile.groups import GROUP_REQUESTER, assign_user_to_group
from profile.models import UserProfile


def get_profile(user):
    """
    Return the ``UserProfile`` for ``user``, creating one if needed.

    New profiles are auto-assigned to the Requester group. Returns ``None``
    for anonymous users.
    """
    if not user.is_authenticated:
        return None
    profile, created = UserProfile.objects.get_or_create(user=user)
    if created:
        assign_user_to_group(user, GROUP_REQUESTER)
    return profile
