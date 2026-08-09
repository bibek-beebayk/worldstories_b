from django.contrib import admin, messages
from django.db import transaction
from django.utils.html import strip_tags
from django.utils import timezone
from django.utils.text import slugify
from .models import Audio, Story, Genre, Tag, Author, Chapter, Review, Submission, StoryView


class ChapterInline(admin.TabularInline):
    model = Chapter
    extra = 1
    verbose_name = "Chapter"
    verbose_name_plural = "Chapters (Quick Add)"
    prepopulated_fields = {"slug": ("title",)}
    fields = ("order", "title", "slug", "content_preview")
    readonly_fields = ("content_preview",)
    show_change_link = True

    @admin.display(description="Preview")
    def content_preview(self, obj):
        if not obj or not obj.content:
            return "-"
        text = strip_tags(str(obj.content)).strip()
        if not text:
            return "-"
        return f"{text[:80]}..." if len(text) > 80 else text

class AudioInline(admin.StackedInline):
    model = Audio
    extra = 0
    classes = ("collapse",)
    fields = ("title", "slug", "order", "audio_file")


def _unique_story_slug(title: str) -> str:
    base_slug = slugify(title) or "story"
    slug = base_slug
    index = 2
    while Story.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


def _publish_submission(submission: Submission, reviewer) -> Story:
    story = Story.objects.create(
        title=submission.title,
        slug=_unique_story_slug(submission.title),
        about=submission.about,
        story_type=submission.story_type,
        language=submission.language,
        author=None,
        submitted_by=submission.user,
        site_published_date=timezone.now().date(),
        cover_image=submission.cover_image or None,
        cover_image_file=submission.cover_image_file,
        pdf_file=submission.pdf_file,
        epub_file=submission.epub_file,
        is_completed=False,
    )
    story.genres.set(submission.genres.all())
    Chapter.objects.create(
        story=story,
        title="Chapter 1",
        slug="chapter-1",
        content=submission.content,
        order=1,
    )
    submission.status = "approved"
    submission.reviewer_notes = submission.reviewer_notes or "Approved and published."
    submission.reviewed_by = reviewer
    submission.reviewed_at = timezone.now()
    submission.published_story = story
    submission.save(
        update_fields=[
            "status",
            "reviewer_notes",
            "reviewed_by",
            "reviewed_at",
            "published_story",
            "updated_at",
        ]
    )
    return story


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    inlines = [ChapterInline, AudioInline]
    change_form_template = "admin/story/story/change_form.html"
    prepopulated_fields = {"slug": ("title",)}
    fieldsets = (
        (
            "Core Details",
            {
                "fields": ("title", "slug", "about", "story_type", "language", "author"),
            },
        ),
        (
            "Summary",
            {
                "fields": ("summary",),
                "description": "Longer-form rich-text summary shown in its own tab on the story page.",
            },
        ),
        (
            "Submission",
            {
                "fields": ("submitted_by",),
            },
        ),
        (
            "Classification",
            {
                "fields": ("genres", "tags", "is_completed"),
            },
        ),
        (
            "Media & Files",
            {
                "fields": ("cover_image", "cover_image_file", "pdf_file", "epub_file"),
            },
        ),
        (
            "Publishing",
            {
                "fields": (
                    ("original_published_year", "original_published_month", "original_published_day"),
                    "site_published_date",
                ),
            },
        ),
        (
            "Read-only Metrics",
            {
                "fields": ("rating", "views", "chapters_count", "audios_count"),
                "classes": ("collapse",),
            },
        ),
    )
    readonly_fields = ("submitted_by", "rating", "views", "chapters_count", "audios_count")
    list_display = (
        "title",
        "story_type",
        "language",
        "author",
        "submitted_by",
        "is_completed",
        "rating",
        "views",
        "original_published_date_display",
        "site_published_date",
        "chapters_count",
        "audios_count",
    )
    search_fields = (
        "title",
        "slug",
        "about",
        "summary",
        "author__name",
        "genres__name",
        "tags__name",
    )
    list_filter = (
        "story_type",
        "language",
        "is_completed",
        "site_published_date",
        "genres",
        "tags",
    )
    ordering = ("-site_published_date", "-id")
    date_hierarchy = "site_published_date"
    list_select_related = ("author", "submitted_by")
    filter_horizontal = ("genres", "tags")
    autocomplete_fields = ("author",)

    @admin.display(description="Chapters")
    def chapters_count(self, obj):
        return obj.chapters.count()

    @admin.display(description="Audios")
    def audios_count(self, obj):
        return obj.audios.count()


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    pass


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    pass


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    search_fields = ("name", "bio")


@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Chapter Info", {"fields": ("story", "order", "title", "slug")}),
        ("Content", {"fields": ("content",)}),
    )
    list_display = ("title", "story", "order", "slug")
    search_fields = ("title", "slug", "story__title")
    list_filter = ("story",)
    list_select_related = ("story",)
    autocomplete_fields = ("story",)
    ordering = ("story__title", "order")

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        story_id = request.GET.get("story")
        if story_id:
            initial["story"] = story_id
        return initial


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("story", "user", "rating", "created_at")
    search_fields = ("story__title", "user__email", "comment")


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "language", "status", "published_story", "created_at", "updated_at")
    search_fields = ("title", "user__email", "about")
    list_filter = ("status", "story_type", "language", "created_at")
    filter_horizontal = ("genres",)
    readonly_fields = ("reviewed_by", "reviewed_at", "published_story", "created_at", "updated_at")
    actions = ["approve_and_publish", "mark_rejected"]

    @admin.action(description="Approve and publish selected submissions")
    def approve_and_publish(self, request, queryset):
        published_count = 0
        skipped_count = 0

        for submission in queryset.prefetch_related("genres"):
            if submission.published_story_id:
                skipped_count += 1
                continue
            with transaction.atomic():
                _publish_submission(submission, request.user)
                published_count += 1

        if published_count:
            self.message_user(
                request, f"{published_count} submission(s) approved and published."
            )
        if skipped_count:
            self.message_user(
                request,
                f"{skipped_count} submission(s) skipped because they were already published.",
                level=messages.WARNING,
            )

    @admin.action(description="Mark selected submissions as rejected")
    def mark_rejected(self, request, queryset):
        now = timezone.now()
        updated_count = 0
        skipped_count = 0

        for submission in queryset:
            if submission.published_story_id:
                skipped_count += 1
                continue
            submission.status = "rejected"
            submission.reviewed_by = request.user
            submission.reviewed_at = now
            if not submission.reviewer_notes:
                submission.reviewer_notes = "Rejected by reviewer."
            submission.save(
                update_fields=[
                    "status",
                    "reviewed_by",
                    "reviewed_at",
                    "reviewer_notes",
                    "updated_at",
                ]
            )
            updated_count += 1

        if updated_count:
            self.message_user(request, f"{updated_count} submission(s) marked as rejected.")
        if skipped_count:
            self.message_user(
                request,
                f"{skipped_count} submission(s) skipped because they are already published.",
                level=messages.WARNING,
            )


@admin.register(StoryView)
class StoryViewAdmin(admin.ModelAdmin):
    list_display = ("story", "ip_address", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("story__title", "ip_address")
    autocomplete_fields = ("story",)
    readonly_fields = ("story", "user", "ip_address", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
