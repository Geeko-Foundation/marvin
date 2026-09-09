from profile.decorators import get_profile


def user_profile(request):
    if request.user.is_authenticated:
        return {'profile': get_profile(request.user)}
    return {'profile': None}
