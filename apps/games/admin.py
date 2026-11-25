from django.contrib import admin

from apps.games.models import DailyPuzzle, PuzzleAttempt, PuzzleBook


@admin.register(PuzzleBook)
class PuzzleBookAdmin(admin.ModelAdmin):
    list_display = ("title", "author_name", "country", "original_published_year", "length_label", "created_at")
    list_filter = ("length_label", "country")
    search_fields = ("title", "author_name")
    filter_horizontal = ("genres",)


@admin.register(DailyPuzzle)
class DailyPuzzleAdmin(admin.ModelAdmin):
    list_display = ("date", "number", "answer_title", "answer_source", "difficulty", "auto_filled", "created_at")
    list_filter = ("difficulty", "auto_filled")
    search_fields = ("story__title", "book__title", "book__author_name", "opening_line")
    raw_id_fields = ("story", "book", "source_chapter", "created_by")
    date_hierarchy = "date"

    @admin.display(description="Answer")
    def answer_title(self, obj):
        return obj.answer_facts.title if (obj.story_id or obj.book_id) else "—"

    @admin.display(description="Source")
    def answer_source(self, obj):
        return "Catalogue" if obj.story_id else "External"


@admin.register(PuzzleAttempt)
class PuzzleAttemptAdmin(admin.ModelAdmin):
    list_display = ("puzzle", "user", "guest_key", "status", "guess_count", "completed_at", "created_at")
    list_filter = ("status",)
    search_fields = ("user__email", "user__username", "guest_key", "puzzle__story__title", "puzzle__book__title")
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
