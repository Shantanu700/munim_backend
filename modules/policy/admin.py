from django.contrib import admin
from django.utils import timezone

from modules.policy.choices import RuleState
from modules.policy.models import Mandate, PolicyRule, StorePolicy


@admin.register(Mandate)
class MandateAdmin(admin.ModelAdmin):
    list_display = ("subject", "tenant", "state", "per_txn", "spent", "total", "expires_at")
    list_filter = ("tenant", "kid")
    search_fields = ("subject", "jti")
    readonly_fields = ("jti", "kid", "token", "spent_paise", "created_at")

    @admin.display(description="state")
    def state(self, obj):
        if obj.revoked_at:
            return "revoked"
        if not obj.is_live(timezone.now()):
            return "expired"
        return "live"

    @admin.display(description="per txn")
    def per_txn(self, obj):
        return f"{obj.per_txn_cap_paise / 100:,.2f}"

    @admin.display(description="spent")
    def spent(self, obj):
        return f"{obj.spent_paise / 100:,.2f}"

    @admin.display(description="total")
    def total(self, obj):
        return f"{obj.total_cap_paise / 100:,.2f}"


class PolicyRuleInline(admin.TabularInline):
    model = PolicyRule
    extra = 0
    fields = ("kind", "state", "on_breach", "config", "first_enforced_at")
    readonly_fields = ("kind", "first_enforced_at")
    can_delete = False

    def has_add_permission(self, request, obj):
        return False


@admin.register(StorePolicy)
class StorePolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "merchant", "is_active", "enforcing_count", "updated_at")
    list_filter = ("is_active", "merchant")
    search_fields = ("name", "merchant__domain")
    inlines = [PolicyRuleInline]

    @admin.display(description="enforcing")
    def enforcing_count(self, obj):
        return obj.rules.filter(state=RuleState.ENFORCING).count()
