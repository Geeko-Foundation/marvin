"""Default permission helpers for the payments app."""

def allow_staff(user) -> bool:
    return user.is_authenticated and user.is_staff
