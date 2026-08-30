import calendar
import uuid

from django.db import models
from django_ckeditor_5.fields import CKEditor5Field
from django.core.validators import FileExtensionValidator, MinValueValidator, MaxValueValidator
from django.conf import settings
from django.utils import timezone
from solo.models import SingletonModel
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

# ISO 3166-1 alpha-2 codes — the single shared source of truth for "country
# of origin" on both Story and StoryQueue (StoryQueue.country is copied
# verbatim into Story.country when a queue entry is turned into a story, so
# both fields must accept exactly the same set of codes). A fixed list
# rather than a Genre/Category-style model, matching how LANGUAGE_CHOICES
# already works here — countries are a well-known, mostly static real-world
# set, not something admins curate over time (unlike story types, which are
# a real StoryType model precisely because admins DO want to curate that set).
COUNTRY_CHOICES = [
    ("AF", "Afghanistan"), ("AL", "Albania"), ("DZ", "Algeria"), ("AD", "Andorra"),
    ("AO", "Angola"), ("AG", "Antigua and Barbuda"), ("AR", "Argentina"), ("AM", "Armenia"),
    ("AU", "Australia"), ("AT", "Austria"), ("AZ", "Azerbaijan"), ("BS", "Bahamas"),
    ("BH", "Bahrain"), ("BD", "Bangladesh"), ("BB", "Barbados"), ("BY", "Belarus"),
    ("BE", "Belgium"), ("BZ", "Belize"), ("BJ", "Benin"), ("BT", "Bhutan"),
    ("BO", "Bolivia"), ("BA", "Bosnia and Herzegovina"), ("BW", "Botswana"), ("BR", "Brazil"),
    ("BN", "Brunei"), ("BG", "Bulgaria"), ("BF", "Burkina Faso"), ("BI", "Burundi"),
    ("CV", "Cabo Verde"), ("KH", "Cambodia"), ("CM", "Cameroon"), ("CA", "Canada"),
    ("CF", "Central African Republic"), ("TD", "Chad"), ("CL", "Chile"), ("CN", "China"),
    ("CO", "Colombia"), ("KM", "Comoros"), ("CG", "Congo"), ("CD", "Congo (DRC)"),
    ("CR", "Costa Rica"), ("CI", "Côte d'Ivoire"), ("HR", "Croatia"), ("CU", "Cuba"),
    ("CY", "Cyprus"), ("CZ", "Czechia"), ("DK", "Denmark"), ("DJ", "Djibouti"),
    ("DM", "Dominica"), ("DO", "Dominican Republic"), ("EC", "Ecuador"), ("EG", "Egypt"),
    ("SV", "El Salvador"), ("GQ", "Equatorial Guinea"), ("ER", "Eritrea"), ("EE", "Estonia"),
    ("SZ", "Eswatini"), ("ET", "Ethiopia"), ("FJ", "Fiji"), ("FI", "Finland"),
    ("FR", "France"), ("GA", "Gabon"), ("GM", "Gambia"), ("GE", "Georgia"),
    ("DE", "Germany"), ("GH", "Ghana"), ("GR", "Greece"), ("GD", "Grenada"),
    ("GT", "Guatemala"), ("GN", "Guinea"), ("GW", "Guinea-Bissau"), ("GY", "Guyana"),
    ("HT", "Haiti"), ("HN", "Honduras"), ("HU", "Hungary"), ("IS", "Iceland"),
    ("IN", "India"), ("ID", "Indonesia"), ("IR", "Iran"), ("IQ", "Iraq"),
    ("IE", "Ireland"), ("IL", "Israel"), ("IT", "Italy"), ("JM", "Jamaica"),
    ("JP", "Japan"), ("JO", "Jordan"), ("KZ", "Kazakhstan"), ("KE", "Kenya"),
    ("KI", "Kiribati"), ("KP", "Korea (North)"), ("KR", "Korea (South)"), ("KW", "Kuwait"),
    ("KG", "Kyrgyzstan"), ("LA", "Laos"), ("LV", "Latvia"), ("LB", "Lebanon"),
    ("LS", "Lesotho"), ("LR", "Liberia"), ("LY", "Libya"), ("LI", "Liechtenstein"),
    ("LT", "Lithuania"), ("LU", "Luxembourg"), ("MG", "Madagascar"), ("MW", "Malawi"),
    ("MY", "Malaysia"), ("MV", "Maldives"), ("ML", "Mali"), ("MT", "Malta"),
    ("MH", "Marshall Islands"), ("MR", "Mauritania"), ("MU", "Mauritius"), ("MX", "Mexico"),
    ("FM", "Micronesia"), ("MD", "Moldova"), ("MC", "Monaco"), ("MN", "Mongolia"),
    ("ME", "Montenegro"), ("MA", "Morocco"), ("MZ", "Mozambique"), ("MM", "Myanmar"),
    ("NA", "Namibia"), ("NR", "Nauru"), ("NP", "Nepal"), ("NL", "Netherlands"),
    ("NZ", "New Zealand"), ("NI", "Nicaragua"), ("NE", "Niger"), ("NG", "Nigeria"),
    ("MK", "North Macedonia"), ("NO", "Norway"), ("OM", "Oman"), ("PK", "Pakistan"),
    ("PW", "Palau"), ("PS", "Palestine"), ("PA", "Panama"), ("PG", "Papua New Guinea"),
    ("PY", "Paraguay"), ("PE", "Peru"), ("PH", "Philippines"), ("PL", "Poland"),
    ("PT", "Portugal"), ("QA", "Qatar"), ("RO", "Romania"), ("RU", "Russia"),
    ("RW", "Rwanda"), ("KN", "Saint Kitts and Nevis"), ("LC", "Saint Lucia"),
    ("VC", "Saint Vincent and the Grenadines"), ("WS", "Samoa"), ("SM", "San Marino"),
    ("ST", "Sao Tome and Principe"), ("SA", "Saudi Arabia"), ("SN", "Senegal"), ("RS", "Serbia"),
    ("SC", "Seychelles"), ("SL", "Sierra Leone"), ("SG", "Singapore"), ("SK", "Slovakia"),
    ("SI", "Slovenia"), ("SB", "Solomon Islands"), ("SO", "Somalia"), ("ZA", "South Africa"),
    ("SS", "South Sudan"), ("ES", "Spain"), ("LK", "Sri Lanka"), ("SD", "Sudan"),
    ("SR", "Suriname"), ("SE", "Sweden"), ("CH", "Switzerland"), ("SY", "Syria"),
    ("TW", "Taiwan"), ("TJ", "Tajikistan"), ("TZ", "Tanzania"), ("TH", "Thailand"),
    ("TL", "Timor-Leste"), ("TG", "Togo"), ("TO", "Tonga"), ("TT", "Trinidad and Tobago"),
    ("TN", "Tunisia"), ("TR", "Turkey"), ("TM", "Turkmenistan"), ("TV", "Tuvalu"),
    ("UG", "Uganda"), ("UA", "Ukraine"), ("AE", "United Arab Emirates"), ("GB", "United Kingdom"),
    ("US", "United States"), ("UY", "Uruguay"), ("UZ", "Uzbekistan"), ("VU", "Vanuatu"),
    ("VA", "Vatican City"), ("VE", "Venezuela"), ("VN", "Vietnam"), ("YE", "Yemen"),
    ("ZM", "Zambia"), ("ZW", "Zimbabwe"),
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


def format_original_published_date(year, month, day):
    """Formats whichever of year/month/day are actually known — "1920",
    "March 1920", or "March 15, 1920". Returns None if even the year is
    missing. Shared by Story.original_published_date_display and
    StoryQueue.published_date_display, since both track the same
    split year/month/day shape."""
    if not year:
        return None
    if month:
        month_name = calendar.month_name[month]
        if day:
            return f"{month_name} {day}, {year}"
        return f"{month_name} {year}"
    return str(year)


class Genre(models.Model):
    name = models.CharField(max_length=100)
    # Unique per-genre URL segment for /genre/<slug> landing pages — same
    # shape/reasoning as Tag.slug above (blank=True for admin JS
    # prepopulation; always populated by admin/API-side slugging). null=True
    # (unlike Tag/Theme) because Genre has far more creation call sites
    # scattered across the codebase — tests, other apps, possibly future
    # code — that legitimately don't need a slug (nothing not exposed via
    # /genre/<slug> cares). NULL, not "", so two such rows don't collide on
    # the unique constraint the way two blank strings would.
    slug = models.SlugField(max_length=60, unique=True, blank=True, null=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Category(models.Model):
    """A second, independent browsing taxonomy alongside Genre — e.g. for a
    homepage/discover "browse by category" surface distinct from the genre
    filter. Same shape as Genre (many-to-many, admin-managed) on purpose."""

    name = models.CharField(max_length=100, unique=True)
    # See Genre.slug's comment for why this is null=True (unlike Tag/Theme).
    slug = models.SlugField(max_length=60, unique=True, blank=True, null=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class StoryType(models.Model):
    """A story's format/category (Novel, Poetry, Short Story, ...) — an
    admin-managed model, same shape as Category, rather than a fixed choices
    list, so new types can be added/renamed/removed from the admin panel
    without a code change."""

    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


def default_story_type_id():
    # Preserves the old CharField's default="Short Story" behavior for
    # Story/Submission — "Short Story" is always present (seeded by the
    # story_type migration). Returns None (leaving the FK unset, caught by
    # the NOT NULL constraint) only if that row was somehow deleted.
    return StoryType.objects.filter(name="Short Story").values_list("id", flat=True).first()


class Tag(models.Model):
    name = models.CharField(max_length=50)
    # Unique per-tag URL segment for /tag/<slug> landing pages. blank=True
    # so the admin's prepopulated_fields (JS-side, from `name`) can leave it
    # empty in the form; the actual value always comes from admin/API-side
    # slugging, never left blank in the DB (see _unique_tag_slug).
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    # Short hand-written intro shown at the top of the /tag/<slug> page, so
    # it reads as real content rather than a bare auto-generated story list.
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Theme(models.Model):
    """Reader-facing emotional register / real-world subject matter of a
    story (grief, coming of age, colonialism, ...) — independent of Tag
    (search-phrase keywords) even though the shape is identical; see Tag's
    URL/slug comment above for why slug is blank=True at the model level."""

    name = models.CharField(max_length=50)
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    description = models.TextField(blank=True)

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
    # Status/source tracking for Claude-generated summary/retrospective text
    # (apps/story/ai_generation.py, ai_generation_jobs.py). "source" is a
    # transparency flag — "metadata" means Claude wrote this from title/
    # author alone (its own general knowledge, not this book's actual text),
    # "content" means it was grounded in this story's real chapter content.
    GEN_STATUS_PENDING = "pending"
    GEN_STATUS_PROCESSING = "processing"
    GEN_STATUS_COMPLETED = "completed"
    GEN_STATUS_FAILED = "failed"
    GEN_STATUS_CHOICES = [
        (GEN_STATUS_PENDING, "Pending"),
        (GEN_STATUS_PROCESSING, "Processing"),
        (GEN_STATUS_COMPLETED, "Completed"),
        (GEN_STATUS_FAILED, "Failed"),
    ]
    GEN_SOURCE_METADATA = "metadata"
    GEN_SOURCE_CONTENT = "content"
    GEN_SOURCE_CHOICES = [
        (GEN_SOURCE_METADATA, "Metadata only (title/author)"),
        (GEN_SOURCE_CONTENT, "Full chapter content"),
    ]

    title = models.CharField(max_length=256)
    # content = models.TextField()
    slug = models.SlugField(max_length=256, unique=True)
    about = models.TextField(blank=True, null=True)
    summary = CKEditor5Field('Text', config_name='extends', blank=True, null=True)
    summary_status = models.CharField(max_length=16, choices=GEN_STATUS_CHOICES, blank=True, null=True)
    summary_source = models.CharField(max_length=16, choices=GEN_SOURCE_CHOICES, blank=True, null=True)
    summary_confident = models.BooleanField(blank=True, null=True)
    summary_confidence_note = models.TextField(blank=True, null=True)
    summary_error = models.TextField(blank=True, null=True)
    retrospective = CKEditor5Field('Text', config_name='extends', blank=True, null=True)
    retrospective_status = models.CharField(max_length=16, choices=GEN_STATUS_CHOICES, blank=True, null=True)
    retrospective_source = models.CharField(max_length=16, choices=GEN_SOURCE_CHOICES, blank=True, null=True)
    retrospective_confident = models.BooleanField(blank=True, null=True)
    retrospective_confidence_note = models.TextField(blank=True, null=True)
    retrospective_error = models.TextField(blank=True, null=True)
    genres = models.ManyToManyField(Genre, related_name="stories")
    categories = models.ManyToManyField(Category, related_name="stories", blank=True)
    story_type = models.ForeignKey(
        StoryType, on_delete=models.PROTECT, related_name="stories", default=default_story_type_id
    )
    language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, default="en", db_index=True)
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES, blank=True)
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
    # related_name explicit to match genres/categories below — without it
    # the reverse accessor defaults to tag.story_set, but TagSerializer /
    # TagViewSet / the sitemap all assume tag.stories, same as genres/categories.
    tags = models.ManyToManyField(Tag, blank=True, related_name="stories")
    themes = models.ManyToManyField(Theme, blank=True, related_name="stories")
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

    def has_video(self):
        return self.videos.exists()

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
        return format_original_published_date(
            self.original_published_year, self.original_published_month, self.original_published_day
        )

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


class StoryQueue(models.Model):
    """A backlog of book ideas an admin plans to eventually publish. Only
    title is required — everything else here mirrors the matching Story
    field exactly (same choices, same split year/month/day shape) so it can
    be copied straight across when the entry is turned into a real (draft,
    unpublished) Story via the "add" action on StoryQueueViewSet — that's
    what flips is_added and fills in added_story. author_name is optional
    to match Story.author, which is itself nullable.

    epub_link/pdf_link/cover_image_link are just reference URLs (e.g. a
    Project Gutenberg page) for wherever the admin found this public-domain
    work — deliberately NOT auto-downloaded into Story's real epub_file/
    pdf_file uploads (fetching an admin-supplied URL server-side is an SSRF
    risk not worth taking on here); cover_image_link is the one exception,
    copied directly into Story.cover_image, since that's already a plain
    URLField on Story rather than an upload."""

    title = models.CharField(max_length=256)
    author_name = models.CharField(max_length=256, blank=True)
    about = models.TextField(blank=True, null=True)
    # The full story text — only meaningful for a short, single-chapter
    # work. When the "add" action turns this entry into a real Story, a
    # non-blank value here becomes that Story's one chapter (see
    # StoryQueueViewSet.add_to_stories) rather than living on Story itself,
    # since a queue entry has no chapter structure of its own to copy into.
    content = models.TextField(blank=True)
    # Admin-only scratch notes about this queue entry (e.g. where it was
    # sourced, quality concerns) — deliberately NOT copied onto the Story
    # by StoryQueueViewSet.add_to_stories, unlike every other field here.
    notes = models.TextField(blank=True)
    story_type = models.ForeignKey(
        StoryType, on_delete=models.PROTECT, null=True, blank=True, related_name="queue_items"
    )
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES, blank=True)
    language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, blank=True)
    genres = models.ManyToManyField(Genre, blank=True, related_name="queue_items")
    categories = models.ManyToManyField(Category, blank=True, related_name="queue_items")
    tags = models.ManyToManyField(Tag, blank=True, related_name="queue_items")
    themes = models.ManyToManyField(Theme, blank=True, related_name="queue_items")
    original_published_year = models.PositiveSmallIntegerField(blank=True, null=True)
    original_published_month = models.PositiveSmallIntegerField(
        blank=True, null=True, validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    original_published_day = models.PositiveSmallIntegerField(
        blank=True, null=True, validators=[MinValueValidator(1), MaxValueValidator(31)]
    )
    epub_link = models.URLField(blank=True)
    pdf_link = models.URLField(blank=True)
    cover_image_link = models.URLField(blank=True)
    is_added = models.BooleanField(default=False)
    added_story = models.ForeignKey(
        Story, on_delete=models.SET_NULL, null=True, blank=True, related_name="queue_source"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} by {self.author_name}" if self.author_name else self.title

    def published_date_display(self):
        return format_original_published_date(
            self.original_published_year, self.original_published_month, self.original_published_day
        )


class EpubImportJob(TimeStampModel):
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="epub_import_jobs")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    error_message = models.TextField(blank=True, null=True)
    chapters_created = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.story.title} - epub import ({self.status})"


class BookFetchJob(TimeStampModel):
    """Tracks one "Fetch Book Data" Story Queue admin action — asking Claude
    to suggest requested_count public-domain books not already in Story/
    StoryQueue, then creating new StoryQueue rows from whatever survives
    dedup (see apps/story/book_fetch_jobs.py). Not tied to a single Story
    (unlike EpubImportJob) — this is a queue-wide, one-off operation."""

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    requested_count = models.PositiveSmallIntegerField()
    created_count = models.PositiveSmallIntegerField(default=0)
    skipped_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Book fetch ({self.status}) — requested {self.requested_count}"


class PromptSettings(SingletonModel):
    """Admin-tunable instructions for Claude-generated Summary/Retrospective
    text (apps/story/ai_generation.py). Deliberately holds only the
    instructional/system-prompt portion — title/author/chapter content are
    assembled into the actual request by code, never templated from this
    text, so editing this can't reshape the request's structure or leak
    unintended instructions into the data-assembly part of the prompt."""

    MODEL_CHOICES = [
        ("claude-opus-5", "Claude Opus 5 (most capable, most expensive)"),
        ("claude-sonnet-5", "Claude Sonnet 5 (balanced)"),
        ("claude-haiku-4-5", "Claude Haiku 4.5 (fastest, cheapest)"),
    ]

    summary_instructions = models.TextField(
        default=(
            "Write a concise, spoiler-free 2-3 paragraph summary of this book for "
            "potential readers browsing the site. Focus on premise, tone, and what "
            "makes it worth reading — do not reveal the ending."
        ),
        help_text=(
            "Instructions for the 'Generate Summary' admin action. Title, author, "
            "and (if selected) chapter content are assembled separately by code — "
            "do not template those into this text."
        ),
    )
    summary_model = models.CharField(max_length=32, choices=MODEL_CHOICES, default="claude-sonnet-5")
    retrospective_instructions = models.TextField(
        default=(
            "Write a reflective retrospective essay analyzing this book's themes, "
            "historical context, reception, and lasting significance, aimed at "
            "readers who have already finished it (spoilers are fine)."
        ),
        help_text="Instructions for the 'Generate Retrospective' admin action.",
    )
    retrospective_model = models.CharField(max_length=32, choices=MODEL_CHOICES, default="claude-sonnet-5")
    excerpt_instructions = models.TextField(
        default=(
            "Write a compelling, SEO-optimized excerpt (roughly 140-160 characters, "
            "never more than 300) for this blog post. It doubles as the page's meta "
            "description and the teaser shown on the blog list page, so make it "
            "concrete and specific — include the post's actual subject/keywords "
            "rather than generic phrasing — and write it as a single line of plain "
            "text with no markdown, no HTML, and no surrounding quote marks."
        ),
        help_text="Instructions for the 'Generate Excerpt' admin action on blog posts.",
    )
    excerpt_model = models.CharField(max_length=32, choices=MODEL_CHOICES, default="claude-sonnet-5")
    book_fetch_instructions = models.TextField(
        default=(
            "You are helping curate a public-domain literature library. Suggest real, "
            "existing books that are firmly in the public domain (originally published "
            "before 1929, or otherwise clearly public domain in the United States) and "
            "are not already in the provided list of existing titles. Prefer "
            "well-regarded public-domain classics and lesser-known but genuinely "
            "worthwhile works, spread across a range of countries, languages of origin, "
            "and story types — avoid suggesting only the handful of most obvious famous "
            "titles. Write each book's synopsis as a short, SEO-friendly teaser for "
            "readers browsing a library site, without spoiling the ending. Only fill in "
            "a cover image, epub, or PDF link if you are confident it is a genuine, "
            "currently-working, public-domain source (e.g. a Project Gutenberg, "
            "Wikimedia Commons, or Internet Archive URL for that exact edition) — leave "
            "any of those blank rather than guess or fabricate a URL. Leave any other "
            "field blank if you don't have good information for it, rather than "
            "guessing."
        ),
        help_text="Instructions for the 'Fetch Book Data' Story Queue admin action.",
    )
    book_fetch_model = models.CharField(max_length=32, choices=MODEL_CHOICES, default="claude-sonnet-5")

    class Meta:
        verbose_name = "AI Generation Prompt Settings"

    def __str__(self):
        return "AI Generation Prompt Settings"


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
    # Independently editable rich-text content owned by this audio track.
    # Admins may author it directly or copy a chapter into it as a snapshot;
    # there is intentionally no persistent Audio -> Chapter relationship.
    transcript = CKEditor5Field("Transcript", config_name="extends", blank=True)
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


class Video(models.Model):
    """A YouTube-hosted video narration of a story. One story can have many
    videos (ordered), mirroring Audio — but the media lives on YouTube, so we
    store only the video id / URL, not a file."""

    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="videos")
    title = models.CharField(max_length=256)
    slug = models.SlugField(max_length=256, null=True)
    # Canonical 11-char YouTube id, stored bare (derived from youtube_url).
    youtube_id = models.CharField(max_length=32)
    # The original URL an admin pasted, kept for reference / editing.
    youtube_url = models.URLField(max_length=512)
    order = models.PositiveIntegerField(default=1)
    # Admin-entered (optional); refined client-side from the IFrame Player API
    # once a viewer actually plays the video.
    duration_seconds = models.FloatField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Video for {self.story.title}: {self.title}"

    def save(self, *args, **kwargs):
        # Safety net so videos added via the Django admin inline (which does not
        # go through VideoAdminSerializer) still get a usable youtube_id.
        if self.youtube_url and not self.youtube_id:
            from .youtube import parse_youtube_id

            parsed = parse_youtube_id(self.youtube_url)
            if parsed:
                self.youtube_id = parsed
        return super().save(*args, **kwargs)

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
    story_type = models.ForeignKey(
        StoryType, on_delete=models.PROTECT, related_name="submissions", default=default_story_type_id
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


def published_blog_q(prefix=""):
    """Mirrors published_story_q() — is_published True AND (publish_at unset
    OR publish_at already passed)."""
    field = f"{prefix}__" if prefix else ""
    return models.Q(**{f"{field}is_published": True}) & (
        models.Q(**{f"{field}publish_at__isnull": True})
        | models.Q(**{f"{field}publish_at__lte": timezone.now()})
    )


class BlogQuerySet(models.QuerySet):
    def published(self):
        return self.filter(published_blog_q())


class Blog(TimeStampModel):
    # Status/source tracking for Claude-generated excerpt text — same
    # transparency-flag meaning as Story's summary/retrospective fields (see
    # Story's own GEN_STATUS_CHOICES/GEN_SOURCE_CHOICES comment).
    GEN_STATUS_PENDING = "pending"
    GEN_STATUS_PROCESSING = "processing"
    GEN_STATUS_COMPLETED = "completed"
    GEN_STATUS_FAILED = "failed"
    GEN_STATUS_CHOICES = [
        (GEN_STATUS_PENDING, "Pending"),
        (GEN_STATUS_PROCESSING, "Processing"),
        (GEN_STATUS_COMPLETED, "Completed"),
        (GEN_STATUS_FAILED, "Failed"),
    ]
    GEN_SOURCE_METADATA = "metadata"
    GEN_SOURCE_CONTENT = "content"
    GEN_SOURCE_CHOICES = [
        (GEN_SOURCE_METADATA, "Metadata only (title/author)"),
        (GEN_SOURCE_CONTENT, "Full post content"),
    ]

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=256, unique=True)
    excerpt = models.CharField(
        max_length=300, blank=True, null=True,
        help_text="Short summary shown on the blog list page and used as the SEO meta description if set.",
    )
    excerpt_status = models.CharField(max_length=16, choices=GEN_STATUS_CHOICES, blank=True, null=True)
    excerpt_source = models.CharField(max_length=16, choices=GEN_SOURCE_CHOICES, blank=True, null=True)
    excerpt_confident = models.BooleanField(blank=True, null=True)
    excerpt_confidence_note = models.TextField(blank=True, null=True)
    excerpt_error = models.TextField(blank=True, null=True)
    content = CKEditor5Field('Text', config_name='extends')
    cover_image_file = VersatileImageField(upload_to="blog_covers/", blank=True, null=True)
    author_name = models.CharField(max_length=150, blank=True, null=True)
    linked_stories = models.ManyToManyField(
        Story,
        blank=True,
        related_name="linked_blog_posts",
        help_text="Optional stories shown after the published post.",
    )
    linked_blogs = models.ManyToManyField(
        "self",
        blank=True,
        symmetrical=False,
        related_name="linked_from_blogs",
        help_text="Optional blog posts shown after this published post.",
    )
    is_published = models.BooleanField(default=True)
    publish_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=(
            "Optional: hide this post from public listings until this moment, "
            "even while is_published is True. Leave blank to publish immediately."
        ),
    )

    objects = BlogQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Blog Post"
        verbose_name_plural = "Blog Posts"

    def __str__(self):
        return self.title
