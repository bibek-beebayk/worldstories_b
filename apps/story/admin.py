from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from .models import Audio, Story, Genre, Tag, Author, Chapter, Review, Submission


class ChapterInline(admin.StackedInline):
    model = Chapter
    extra = 0
    prepopulated_fields = {"slug": ("title",)}

class AudioInline(admin.StackedInline):
    model = Audio
    extra = 0


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
        author=None,
        published_date=timezone.now().date(),
        cover_image=submission.cover_image or None,
        cover_image_file=submission.cover_image_file,
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
    prepopulated_fields = {"slug": ("title",)}


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    pass


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    pass


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    pass


@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    pass


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("story", "user", "rating", "created_at")
    search_fields = ("story__title", "user__email", "comment")


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "status", "published_story", "created_at", "updated_at")
    search_fields = ("title", "user__email", "about")
    list_filter = ("status", "story_type", "created_at")
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
