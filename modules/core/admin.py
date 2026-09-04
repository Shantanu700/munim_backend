from django.contrib import admin

from modules.core.models import PlatformAPIKey



@admin.register(PlatformAPIKey)
class PlatformAPIKeyAdmin(admin.ModelAdmin):
    list_display = ("prefix", "user", "name", "is_live", "last_used_at", "created_at")
    list_filter = ("revoked_at",)
    search_fields = ("prefix", "user__email")
    readonly_fields = ("key_hash", "prefix", "created_at", "last_used_at")
