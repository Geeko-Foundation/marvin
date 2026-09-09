from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from profile.models import UserProfile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    fk_name = 'user'
    can_delete = False
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'country', 'push_notifications_enabled', 'wise_account_id', 'updated_at')
    search_fields = ('user__username', 'user__email', 'country')
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')


class UserAdmin(BaseUserAdmin):
    inlines = [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
