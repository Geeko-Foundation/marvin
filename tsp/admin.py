from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from tsp.models import (
    Comment,
    Document,
    Event,
    Expense,
    StateHistory,
    TravelRequest,
    UserProfile,
)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    fk_name = 'user'
    can_delete = False
    readonly_fields = ('created_at', 'created_by', 'changed_at', 'changed_by')


class UserAdmin(BaseUserAdmin):
    inlines = [UserProfileInline]


class ExpenseInline(admin.TabularInline):
    model = Expense
    extra = 1


class StateHistoryInline(admin.TabularInline):
    model = StateHistory
    extra = 0
    readonly_fields = ('from_state', 'to_state', 'user', 'notes', 'created_at', 'created_by', 'changed_at', 'changed_by')


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('name', 'location', 'start_date', 'end_date', 'accepting_requests', 'changed_at')
    list_filter = ('accepting_requests',)
    readonly_fields = ('created_at', 'created_by', 'changed_at', 'changed_by')


@admin.register(TravelRequest)
class TravelRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'event', 'state', 'changed_at')
    list_filter = ('state', 'event')
    search_fields = ('user__username', 'event__name')
    readonly_fields = ('created_at', 'created_by', 'changed_at', 'changed_by')
    inlines = [ExpenseInline, StateHistoryInline]


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ('request', 'user', 'created_at', 'changed_at')
    readonly_fields = ('created_at', 'created_by', 'changed_at', 'changed_by')


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ('request', 'kind', 'original_name', 'uploaded_by', 'created_at', 'changed_at')
    readonly_fields = ('created_at', 'created_by', 'changed_at', 'changed_by')


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
