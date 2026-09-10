from django.contrib import admin

from tsp.models import (
    Comment,
    Document,
    Event,
    Expense,
    StateHistory,
    TravelRequest,
)


class ExpenseInline(admin.TabularInline):
    """Editable expense lines on the travel request admin change page."""

    model = Expense
    extra = 1


class StateHistoryInline(admin.TabularInline):
    """Read-only state transition log on the travel request admin change page."""

    model = StateHistory
    extra = 0
    readonly_fields = ('from_state', 'to_state', 'user', 'notes', 'created_at', 'created_by', 'updated_at', 'changed_by')


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    """Browse and filter TSP events."""

    list_display = ('name', 'location', 'start_date', 'end_date', 'accepting_requests', 'updated_at')
    list_filter = ('accepting_requests',)
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')


@admin.register(TravelRequest)
class TravelRequestAdmin(admin.ModelAdmin):
    """Browse travel requests with inline expenses and state history."""

    list_display = ('id', 'user', 'event', 'state', 'updated_at')
    list_filter = ('state', 'event')
    search_fields = ('user__username', 'event__name')
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')
    inlines = [ExpenseInline, StateHistoryInline]


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    """Browse request comments."""

    list_display = ('request', 'user', 'created_at', 'updated_at')
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    """Browse uploaded request documents."""

    list_display = ('request', 'kind', 'original_name', 'uploaded_by', 'created_at', 'updated_at')
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')
