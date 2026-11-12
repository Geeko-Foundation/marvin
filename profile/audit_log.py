"""An append-only record of the things people do, kept apart from the data."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class AuditLogEntry(models.Model):
    """
    One thing that happened, written once and never changed.

    The domain models already carry created_by and changed_by, but those move
    with the row and say nothing about what happened in between. This records
    every action, and the application cannot alter the record afterwards.
    """

    action = models.CharField(max_length=60)
    target = models.CharField(max_length=120, blank=True)
    detail = models.TextField(blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action']),
            models.Index(fields=['created_at']),
        ]
        verbose_name_plural = 'audit log entries'

    def __str__(self):
        who = self.user.get_username() if self.user else 'system'
        return f'{self.created_at:%Y-%m-%d %H:%M} {who} {self.action} {self.target}'.strip()

    def save(self, *args, **kwargs):
        """Allow the first write and refuse every later one."""
        if self.pk is not None:
            raise ValueError('Audit log entries cannot be changed once written.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Refuse deletion; a trail with gaps is not a trail."""
        raise ValueError('Audit log entries cannot be deleted.')


def record(action: str, *, user=None, target='', detail='') -> AuditLogEntry | None:
    """
    Write one entry.

    Logging must never be the reason an action fails, so a failure here is
    swallowed rather than raised.
    """
    try:
        return AuditLogEntry.objects.create(
            action=action,
            target=str(target)[:120],
            detail=str(detail),
            user=user if getattr(user, 'is_authenticated', False) else None,
        )
    except Exception:  # pragma: no cover - logging must not break the caller
        import logging

        logging.getLogger('marvin').exception('Could not write audit entry %s', action)
        return None
