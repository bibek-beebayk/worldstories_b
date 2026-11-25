from django.contrib import admin

from apps.games.models import DailyPuzzle, PuzzleAttempt


@admin.register(DailyPuzzle)
class DailyPuzzleAdmin(admin.ModelAdmin):
    list_display = ("date", "number", "story", "difficulty", "auto_filled", "created_at")
    list_filter = ("difficulty", "auto_filled")
    search_fields = ("story__title", "opening_line")
    raw_id_fields = ("story", "source_chapter", "created_by")
    date_hierarchy = "date"


@admin.register(PuzzleAttempt)
class PuzzleAttemptAdmin(admin.ModelAdmin):
    list_display = ("puzzle", "user", "guest_key", "status", "guess_count", "completed_at", "created_at")
    list_filter = ("status",)
    search_fields = ("user__email", "user__username", "guest_key", "puzzle__story__title")
    readonly_fields = (
        "puzzle",
        "user",
        "guest_key",
        "guesses",
        "status",
        "guess_count",
        "completed_at",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
