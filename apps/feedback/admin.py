from django.contrib import admin

from .models import Feedback, FeedbackImage


class FeedbackImageInline(admin.TabularInline):
    model = FeedbackImage
    extra = 0
    readonly_fields = ("image", "created_at")


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    """Fallback only — feedback is triaged in the React admin panel (/admin/feedback)."""

    inlines = [FeedbackImageInline]

    list_display = ("category", "short_message", "user", "email", "rating", "status", "created_at")
    list_filter = ("status", "category", "rating")
    search_fields = ("message", "email", "user__email")
    readonly_fields = ("user", "email", "category", "rating", "message", "page_url", "user_agent", "created_at")

    @admin.display(description="Message")
    def short_message(self, obj):
        return obj.message[:80]
