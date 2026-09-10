"""Default permission helpers for the payments app."""


def allow_staff(user) -> bool:
    """Return True when ``user`` is authenticated and has ``is_staff`` set."""
    return user.is_authenticated and user.is_staff
