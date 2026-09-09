from django import template

register = template.Library()

BADGE_MAP = {
    'draft': 'badge-ghost',
    'submitted': 'badge-info',
    'approved': 'badge-warning',
    'rejected': 'badge-error',
    'accepted': 'badge-primary',
    'submitted_for_payment': 'badge-secondary',
    'paid': 'badge-success',
    'cancelled': 'badge-neutral',
}


@register.filter
def state_badge(state):
    return BADGE_MAP.get(state, 'badge-ghost')
