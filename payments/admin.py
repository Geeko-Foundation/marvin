from django.contrib import admin

from payments.models import PaymentRecord


@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):
    list_display = (
        'reference', 'gateway_id', 'amount', 'currency',
        'status', 'recipient_name', 'created_by', 'changed_by', 'created_at',
    )
    list_filter = ('gateway_id', 'status', 'currency')
    search_fields = ('reference', 'recipient_name', 'recipient_email', 'external_id')
    readonly_fields = ('created_at', 'created_by', 'updated_at', 'changed_by')
