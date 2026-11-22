from django.contrib import admin

from modules.ledger.models import LedgerEntry


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    """Read-only on purpose: the ledger is append-only in Postgres (ledger/0002).

    Without these three hooks the admin still renders a Save and a Delete button,
    and pressing either one earns a 500 from the trigger rather than a refusal the
    user can understand. Entries are created by LedgerEntry.append(), never here.
    """
    list_display = ("seq", "action", "decision", "reason_code", "tenant", "created_at")
    list_filter = ("decision", "action", "tenant")
    readonly_fields = ("seq", "prev_hash", "chain_hash", "detail")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
