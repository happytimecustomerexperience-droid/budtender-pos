from django.contrib import admin

from .models import Customer, DutchieWriteAudit


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("id", "first_name", "last_name", "phone", "dutchie_acct_id", "over_21", "updated_at")
    search_fields = ("phone", "last_name", "first_name", "dutchie_acct_id")
    list_filter = ("over_21", "state")


@admin.register(DutchieWriteAudit)
class DutchieWriteAuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "store", "action", "ok", "acct_id", "shipment_id", "username", "summary")
    search_fields = ("store", "action", "username", "summary", "acct_id", "shipment_id")
    list_filter = ("ok", "store", "action")
    readonly_fields = ("store", "action", "acct_id", "shipment_id", "summary", "ok", "username", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
