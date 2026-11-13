from django.contrib import admin

from payments.models import PaymentRecord, WiseLogEntry


@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):
    """Read-only audit fields; filter and search payment history in Django admin."""

    list_display = (
        'reference', 'gateway_id', 'amount', 'currency',
        'status', 'recipient_name', 'created_by', 'changed_by', 'created_at',
    )
    list_filter = ('gateway_id', 'status', 'currency')
    search_fields = ('reference', 'recipient_name', 'recipient_email', 'external_id')
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')


@admin.register(WiseLogEntry)
class WiseLogEntryAdmin(admin.ModelAdmin):
    """Every exchange with Wise, readable and never editable."""

    list_display = ('created_at', 'kind', 'mode', 'method', 'path', 'status_code', 'transfer_id', 'duration_ms')
    list_filter = ('kind', 'mode', 'status_code')
    search_fields = ('path', 'transfer_id')
    readonly_fields = [field.name for field in WiseLogEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
