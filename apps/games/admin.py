from django.contrib import admin

from apps.games.models import DailyPuzzle, PuzzleAttempt, PuzzleRotation


@admin.register(DailyPuzzle)
class DailyPuzzleAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "puzzle_type",
        "answer_kind",
        "answer_label",
        "status",
        "difficulty",
        "auto_filled",
        "created_at",
    )
    list_filter = ("puzzle_type", "answer_kind", "status", "difficulty", "auto_filled")
    search_fields = ("answer_story__title", "answer_author__name")
    date_hierarchy = "date"
    raw_id_fields = (
        "answer_story",
        "answer_author",
        "source_chapter",
        "created_by",
        "approved_by",
    )
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Answer")
    def answer_label(self, obj):
        if obj.answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR and obj.answer_author_id:
            return obj.answer_author.name
        if obj.answer_story_id:
            return obj.answer_story.title
        return "Unassigned"


@admin.register(PuzzleAttempt)
class PuzzleAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "puzzle",
        "user",
        "guest_key",
        "status",
        "guess_count",
        "completed_at",
        "created_at",
    )
    list_filter = ("status", "puzzle__puzzle_type")
    search_fields = ("user__username", "user__email", "guest_key")
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


@admin.register(PuzzleRotation)
class PuzzleRotationAdmin(admin.ModelAdmin):
    list_display = ("weekday", "puzzle_type")
    ordering = ("weekday",)
