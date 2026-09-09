from functools import partial, wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required as django_login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from profile.decorators import get_profile
from profile.groups import user_in_any_group
from tsp.models import TravelRequest

login_required = partial(django_login_required, login_url=settings.APP_LOGIN_URL)


def group_required(*group_names):
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not user_in_any_group(request.user, *group_names):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def staff_or_owner(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, pk, *args, **kwargs):
        travel_request = get_object_or_404(TravelRequest, pk=pk)
        profile = get_profile(request.user)
        is_owner = travel_request.user_id == request.user.id
        if not (is_owner or profile.is_committee):
            raise PermissionDenied
        return view_func(request, pk, travel_request=travel_request, profile=profile, *args, **kwargs)
    return wrapper
