from django.contrib.auth.models import User
from django.urls import reverse
from django.utils.html import format_html, mark_safe
import django_tables2 as tables

from tsp.models import Event


class EventTable(tables.Table):
    name = tables.Column(verbose_name='Event')
    location = tables.Column()
    start_date = tables.DateColumn(format='Y-m-d')
    end_date = tables.DateColumn(format='Y-m-d')
    status = tables.Column(empty_values=(), orderable=False, verbose_name='Status')
    actions = tables.Column(empty_values=(), orderable=False, verbose_name='')

    class Meta:
        model = Event
        fields = ('name', 'location', 'start_date', 'end_date')
        sequence = ('name', 'location', 'start_date', 'end_date', 'status', 'actions')
        attrs = {
            'class': 'table table-zebra w-full',
            'thead': {'class': ''},
        }
        empty_text = 'No events match your search.'
        order_by = '-start_date'

    def render_status(self, record):
        if record.is_past:
            return mark_safe('<span class="badge badge-neutral badge-sm">Past</span>')
        if record.accepting_requests:
            return mark_safe('<span class="badge badge-success badge-sm">Open</span>')
        return mark_safe('<span class="badge badge-warning badge-sm">Closed</span>')

    def render_actions(self, record):
        if record.is_past or not record.accepting_requests:
            return '—'
        url = reverse('request_create') + f'?event={record.pk}'
        return format_html('<a href="{}" class="link link-primary text-sm">Apply</a>', url)


class UserTable(tables.Table):
    username = tables.Column(linkify={
        'viewname': 'admin:auth_user_change',
        'args': [tables.A('pk')],
    })
    full_name = tables.Column(empty_values=(), orderable=False, verbose_name='Name')
    email = tables.EmailColumn()
    groups = tables.Column(empty_values=(), orderable=False)
    country = tables.Column(empty_values=(), orderable=False)
    staff = tables.Column(empty_values=(), orderable=False, verbose_name='Admin')

    class Meta:
        model = User
        sequence = ('username', 'full_name', 'email', 'groups', 'country', 'staff')
        attrs = {
            'class': 'table table-zebra w-full',
        }
        empty_text = 'No users match your search.'
        order_by = 'username'

    def render_full_name(self, record):
        return record.get_full_name() or '—'

    def render_groups(self, record):
        names = [g.name for g in record.groups.all()]
        return ', '.join(names) if names else '—'

    def render_country(self, record):
        profile = getattr(record, 'profile', None)
        return profile.country if profile and profile.country else '—'

    def render_staff(self, record):
        if record.is_superuser:
            return mark_safe('<span class="badge badge-primary badge-sm">Superuser</span>')
        if record.is_staff:
            return mark_safe('<span class="badge badge-secondary badge-sm">Staff</span>')
        return '—'
