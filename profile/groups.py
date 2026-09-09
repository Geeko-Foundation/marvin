"""Django auth groups and permissions for Marvin role-based access."""

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, ProgrammingError

GROUP_REQUESTER = 'Requester'
GROUP_TSP = 'TSP Committee'
GROUP_FINANCE = 'Finance'

ALL_GROUPS = (GROUP_REQUESTER, GROUP_TSP, GROUP_FINANCE)

LEGACY_ROLE_TO_GROUP = {
    'requester': GROUP_REQUESTER,
    'tsp': GROUP_TSP,
    'finance': GROUP_FINANCE,
}

GROUP_PERMISSIONS = {
    GROUP_REQUESTER: (
        'submit_travel_requests',
    ),
    GROUP_TSP: (
        'submit_travel_requests',
        'review_travel_requests',
        'manage_events',
        'view_reports',
    ),
    GROUP_FINANCE: (
        'view_reports',
        'process_payments',
    ),
}


def _profile_permissions() -> dict[str, Permission]:
    from profile.models import UserProfile

    ct = ContentType.objects.get_for_model(UserProfile)
    codenames = {codename for perms in GROUP_PERMISSIONS.values() for codename in perms}
    return {
        codename: Permission.objects.get(codename=codename, content_type=ct)
        for codename in codenames
    }


def ensure_groups() -> None:
    """Create Marvin groups and assign permissions. Idempotent."""
    try:
        perms = _profile_permissions()
    except (OperationalError, ProgrammingError, Permission.DoesNotExist, ContentType.DoesNotExist):
        return

    for group_name in ALL_GROUPS:
        group, _ = Group.objects.get_or_create(name=group_name)
        codenames = GROUP_PERMISSIONS.get(group_name, ())
        group.permissions.set([perms[c] for c in codenames if c in perms])


def user_in_group(user, group_name: str) -> bool:
    return user.is_authenticated and user.groups.filter(name=group_name).exists()


def user_in_any_group(user, *group_names: str) -> bool:
    if not user.is_authenticated:
        return False
    return user.groups.filter(name__in=group_names).exists()


def assign_user_to_group(user, group_name: str, *, replace: bool = False) -> None:
    """Add user to a Marvin group. If replace=True, remove other Marvin groups first."""
    ensure_groups()
    group = Group.objects.get(name=group_name)
    if replace:
        user.groups.remove(*Group.objects.filter(name__in=ALL_GROUPS))
    user.groups.add(group)
