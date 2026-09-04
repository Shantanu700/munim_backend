from django.contrib import admin

from modules.ledger.models import LedgerEntry


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("seq", "action", "decision", "reason_code", "tenant", "created_at")
    list_filter = ("decision", "action", "tenant")
    readonly_fields = ("seq", "prev_hash", "chain_hash", "detail")
