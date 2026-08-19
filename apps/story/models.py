import calendar
import uuid

from django.db import models
from django_ckeditor_5.fields import CKEditor5Field
from django.core.validators import FileExtensionValidator, MinValueValidator, MaxValueValidator
from django.conf import settings
from django.utils import timezone
from versatileimagefield.fields import VersatileImageField

from core.libs.models import TimeStampModel
from core.libs.validators import FileSizeValidator

MAX_DOCUMENT_UPLOAD_SIZE = 50 * 1024 * 1024  # pdf/epub
MAX_AUDIO_UPLOAD_SIZE = 150 * 1024 * 1024


def published_story_q(prefix=""):
    """Q object for "is this story currently publicly visible" — is_published
    is True AND either publish_at isn't set (publish immediately) or it's
    already passed. Takes an optional relation prefix so it works both as
    Story.objects.filter(published_story_q()) and for cross-relation lookups
    like Genre.objects.filter(published_story_q("stories")).
    """
    field = f"{prefix}__" if prefix else ""
    return models.Q(**{f"{field}is_published": True}) & (
        models.Q(**{f"{field}publish_at__isnull": True})
        | models.Q(**{f"{field}publish_at__lte": timezone.now()})
    )


class StoryQuerySet(models.QuerySet):
    def published(self):
        return self.filter(published_story_q())

STORY_TYPE_CHOICES = [
    ("Short Story", "Short Story"),
    ("Novel", "Novel"),
    ("Novella", "Novella"),
    ("Poetry", "Poetry"),
    ("Non Fiction", "Non Fiction"),
    ("Religious Text", "Religious Text"),
    ("Summary", "Summary"),
]

LANGUAGE_CHOICES = [
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("pt", "Portuguese"),
    ("it", "Italian"),
    ("hi", "Hindi"),
    ("ne", "Nepali"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("zh", "Chinese"),
    ("ar", "Arabic"),
    ("ru", "Russian"),
]

SUBMISSION_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("requires_edit", "Requires Edit"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
]

# STORY_STATUS_CHOICES = [
#     ("Draft", "Draft"),
#     ("Published", "Published"),
#     ("Archived", "Archived"),
# ]


class Genre(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Category(models.Model):
    """A second, independent browsing taxonomy alongside Genre — e.g. for a
    homepage/discover "browse by category" surface distinct from the genre
    filter. Same shape as Genre (many-to-many, admin-managed) on purpose."""

    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class Author(models.Model):
    name = models.CharField(max_length=100)
    bio = models.TextField(blank=True, null=True)
    image = models.URLField(blank=True, null=True)

    def stories_count(self):
        return self.stories.count()

    def __str__(self):
        return self.name


class Story(models.Model):
    title = models.CharField(max_length=256)
    # content = models.TextField()
    slug = models.SlugField(max_length=256, unique=True)
    about = models.TextField(blank=True, null=True)
    summary = CKEditor5Field('Text', config_name='extends', blank=True, null=True)
    genres = models.ManyToManyField(Genre, related_name="stories")
    categories = models.ManyToManyField(Category, related_name="stories", blank=True)
    story_type = models.CharField(
        max_length=50, choices=STORY_TYPE_CHOICES, default="Short Story"
    )
    language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, default="en", db_index=True)
    translation_group = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        db_index=True,
        help_text="Stories sharing this value are treated as translations of the same work.",
    )
    author = models.ForeignKey(Author, on_delete=models.CASCADE, blank=True, null=True, related_name="stories")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="submitted_stories",
    )
    original_published_year = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        help_text="Year the work was originally published, if known (e.g. for reprints/adaptations of existing works).",
    )
    original_published_month = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        help_text="Month (1-12) it was originally published, if known. Only meaningful when the year is also set.",
    )
    original_published_day = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Day of month it was originally published, if known. Only meaningful when the month is also set.",
    )
    site_published_date = models.DateField(
        blank=True,
        null=True,
        help_text="When this story went live on WorldStories. Drives recency sorting and the publishing velocity stat.",
    )
    cover_image = models.URLField(blank=True, null=True)
    cover_image_file = VersatileImageField(upload_to="story_covers/", blank=True, null=True)
    pdf_file = models.FileField(
        upload_to="story_files/pdfs/",
        blank=True,
        null=True,
        validators=[
            FileExtensionValidator(allowed_extensions=["pdf"]),
            FileSizeValidator(MAX_DOCUMENT_UPLOAD_SIZE),
        ],
    )
    epub_file = models.FileField(
        upload_to="story_files/epubs/",
        blank=True,
        null=True,
        validators=[
            FileExtensionValidator(allowed_extensions=["epub"]),
            FileSizeValidator(MAX_DOCUMENT_UPLOAD_SIZE),
        ],
    )
    # Estimated reading time (minutes) derived from epub_file/pdf_file, probed
    # once at upload time (StoryAdminSerializer) rather than parsed live from
    # remote storage on every story-detail view — see reading_time.py. Only
    # meaningful for chapterless stories; chapter-based stories compute this
    # live instead since chapter content is already in Postgres.
    cached_file_reading_minutes = models.PositiveIntegerField(blank=True, null=True)
    is_completed = models.BooleanField(default=False)
    tags = models.ManyToManyField(Tag, blank=True)
    rating = models.FloatField(default=0.0)
    views = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)
    publish_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=(
            "Optional: hide this story from public listings/search until this "
            "moment, even while is_published is True. Leave blank to publish "
            "immediately (as soon as is_published is True)."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = StoryQuerySet.as_manager()

    def has_audio(self):
        return self.audios.exists()

    def save(self, *args, **kwargs):
        if self.is_published and not self.site_published_date:
            self.site_published_date = (
                timezone.localdate(self.publish_at)
                if self.publish_at
                else timezone.localdate()
            )
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"site_published_date"}
        return super().save(*args, **kwargs)

    def original_published_date_display(self):
        """Formats whichever of year/month/day are actually known — "1920",
        "March 1920", or "March 15, 1920". Returns None if even the year is
        missing; falling back to site_published_date in that case is handled
        at the serializer level, since that's a different field entirely."""
        if not self.original_published_year:
            return None
        if self.original_published_month:
            month_name = calendar.month_name[self.original_published_month]
            if self.original_published_day:
                return f"{month_name} {self.original_published_day}, {self.original_published_year}"
            return f"{month_name} {self.original_published_year}"
        return str(self.original_published_year)

    def __str__(self):
        return self.title
    
    class Meta:
        verbose_name = "Story"
        verbose_name_plural = "Stories"


def with_preferred_translation_only(queryset, preferred_language=None):
    """Given a queryset of Story rows, keep only one row per translation_group.
    When a language is requested, choose within that language; otherwise use
    the English edition if the group has one, then its oldest (lowest id)
    edition — so public listings (library, home, etc.) show a single
    entry per underlying work instead of one per language. Stories that
    aren't linked to any other translation form a "group" of one and always
    pass through untouched.

    Only considers currently-published (and not still schedule-gated)
    stories when picking each group's representative, so callers should
    already be filtering on the same condition (as every current caller
    does) — otherwise a group could "win" on an edition the outer filter
    would exclude anyway, and vanish from the results entirely.
    """
    candidates = Story.objects.filter(
        published_story_q(),
        translation_group=models.OuterRef("translation_group"),
    )
    if preferred_language and preferred_language != "all":
        candidates = candidates.filter(language=preferred_language)

    preferred_id = (
        candidates
        .annotate(
            _language_priority=models.Case(
                models.When(language="en", then=models.Value(0)),
                default=models.Value(1),
                output_field=models.IntegerField(),
            )
        )
        .order_by("_language_priority", "id")
        .values("id")[:1]
    )
    return queryset.filter(id=models.Subquery(preferred_id))


class Chapter(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="chapters")
    title = models.CharField(max_length=256)
    slug = models.SlugField(max_length=256, null=True)
    content = CKEditor5Field('Text', config_name='extends')
    order = models.PositiveIntegerField()

    class Meta:
        unique_together = ("story", "order")
        ordering = ["order"]

    def __str__(self):
        return f"{self.story.title} - Chapter {self.order}: {self.title}"
    

class Audio(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="audios")
    title = models.CharField(max_length=256)
    slug = models.SlugField(max_length=256, null=True)
    audio_file = models.FileField(
        upload_to='story_audios/',
        validators=[
            FileExtensionValidator(allowed_extensions=["mp3"]),
            FileSizeValidator(MAX_AUDIO_UPLOAD_SIZE),
        ],
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    order = models.PositiveIntegerField(default=1)

    # Probed from the audio file itself (via mutagen) once at upload time and
    # cached here — computing it on demand would mean downloading the full
    # file from remote storage on every story-detail page view.
    duration_seconds = models.FloatField(null=True, blank=True, default=None)
    # Cached at upload time so serializing a story with many audio chapters
    # does not issue one remote object-storage HEAD request per chapter.
    file_size_bytes = models.PositiveBigIntegerField(default=0, editable=False)

    def __str__(self):
        return f"Audio for {self.story.title} uploaded at {self.uploaded_at}"
    
    class Meta:
        unique_together = ("story", "order")
        ordering = ["order"]


class Review(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="story_reviews"
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("story", "user")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self):
        return f"{self.story.title} review by {self.user}"


class Favorite(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="favorites")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="story_favorites"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("story", "user")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self):
        return f"{self.user} favorited {self.story}"


class Submission(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="story_submissions"
    )
    title = models.CharField(max_length=256)
    about = models.TextField()
    content = models.TextField()
    story_type = models.CharField(
        max_length=50, choices=STORY_TYPE_CHOICES, default="Short Story"
    )
    language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, default="en")
    genres = models.ManyToManyField(Genre, related_name="submissions")
    cover_image = models.URLField(blank=True, null=True)
    cover_image_file = models.ImageField(
        upload_to="submission_covers/", blank=True, null=True
    )
    notes = models.TextField(blank=True, null=True)
    pdf_file = models.FileField(
        upload_to="submission_pdfs/",
        blank=True,
        null=True,
        validators=[
            FileExtensionValidator(allowed_extensions=["pdf"]),
            FileSizeValidator(MAX_DOCUMENT_UPLOAD_SIZE),
        ],
    )
    epub_file = models.FileField(
        upload_to="submission_epubs/",
        blank=True,
        null=True,
        validators=[
            FileExtensionValidator(allowed_extensions=["epub"]),
            FileSizeValidator(MAX_DOCUMENT_UPLOAD_SIZE),
        ],
    )
    status = models.CharField(
        max_length=20, choices=SUBMISSION_STATUS_CHOICES, default="pending"
    )
    reviewer_notes = models.TextField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="reviewed_submissions",
    )
    published_story = models.OneToOneField(
        "Story",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="submission",
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["reviewed_at"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.status})"


class StoryView(TimeStampModel):
    """One row per counted view. Used only to de-duplicate repeat visits from the same
    IP within a short window before bumping Story.views — not meant to be queried for
    anything beyond that (see readers_count/ReadingProgress in apps.stats for real
    per-user reading activity)."""

    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="view_events")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="story_view_events",
    )
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["story", "ip_address", "created_at"]),
        ]

    def __str__(self):
        return f"View of {self.story} from {self.ip_address} at {self.created_at}"
