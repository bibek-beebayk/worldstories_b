from django.contrib import admin

from .models import AnalyticsEvent


@admin.register(AnalyticsEvent)
class AnalyticsEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "story",
        "blog",
        "user",
        "visitor_id",
        "duration_seconds",
        "created_at",
    )
    list_filter = ("event_type", "created_at")
    search_fields = (
        "visitor_id",
        "session_id",
        "story__title",
        "story__slug",
        "blog__title",
        "blog__slug",
        "user__email",
    )
    readonly_fields = (
        "event_id",
        "event_type",
        "user",
        "visitor_id",
        "session_id",
        "story",
        "blog",
        "duration_seconds",
        "value",
        "metadata",
        "created_at",
    )
    date_hierarchy = "created_at"
    list_select_related = ("story", "blog", "user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
