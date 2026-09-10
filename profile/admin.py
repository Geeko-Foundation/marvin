from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from profile.models import UserProfile


class UserProfileInline(admin.StackedInline):
    """Editable profile fields on the Django User admin change page."""

    model = UserProfile
    fk_name = 'user'
    can_delete = False
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """Standalone admin for browsing and searching user profiles."""

    list_display = ('user', 'country', 'push_notifications_enabled', 'wise_account_id', 'updated_at')
    search_fields = ('user__username', 'user__email', 'country')
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')


class UserAdmin(BaseUserAdmin):
    """Extend stock User admin with an inline ``UserProfile``."""

    inlines = [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
