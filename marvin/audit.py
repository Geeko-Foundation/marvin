"""Audit fields and request-scoped user for created_by / changed_by tracking."""

from __future__ import annotations

import threading

from django.conf import settings
from django.db import models


_audit_user = threading.local()


def set_current_audit_user(user) -> None:
    _audit_user.value = user


def get_current_audit_user():
    return getattr(_audit_user, 'value', None)


def clear_current_audit_user() -> None:
    if hasattr(_audit_user, 'value'):
        del _audit_user.value


class AuditModel(models.Model):
    """Abstract base with standard audit columns."""

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='%(app_label)s_%(class)s_created',
    )
    changed_at = models.DateTimeField(auto_now=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='%(app_label)s_%(class)s_changed',
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        user = get_current_audit_user()
        if user is not None and getattr(user, 'is_authenticated', False):
            if self._state.adding and not self.created_by_id:
                self.created_by = user
            self.changed_by = user

        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            fields = set(update_fields)
            fields.add('changed_at')
            if self._state.adding:
                fields.add('created_at')
            if self.created_by_id:
                fields.add('created_by')
            if self.changed_by_id:
                fields.add('changed_by')
            kwargs['update_fields'] = list(fields)

        super().save(*args, **kwargs)


class AuditUserMiddleware:
    """Attach the authenticated user for audit field population on save()."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(request, 'user', None) and request.user.is_authenticated:
            set_current_audit_user(request.user)
        else:
            clear_current_audit_user()
        try:
            return self.get_response(request)
        finally:
            clear_current_audit_user()
