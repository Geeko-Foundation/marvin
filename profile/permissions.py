from profile.groups import GROUP_FINANCE, user_in_group


def finance_can_process_payments(user) -> bool:
    if not user.is_authenticated:
        return False
    return user.has_perm('profile.process_payments') or user_in_group(user, GROUP_FINANCE)
