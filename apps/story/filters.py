from django_filters import rest_framework as filters
from django.db.models import Exists, F, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Lower, NullIf
from django.utils.text import slugify
from .models import Author, Chapter, Story, StoryMood, Video

# Length buckets on the whole-story reading estimate (minutes). Lower bound
# inclusive, upper exclusive; matches the Library's "Length" facet.
LENGTH_BUCKETS = {
    "under_10": (None, 10),
    "10_60": (10, 60),
    "1_5h": (60, 300),
    "5h_plus": (300, None),
}
FORMATS = ("read", "listen", "read_along", "watch", "quick_read")


def with_reading_minutes(queryset):
    """Annotates `_minutes`: the same whole-story estimate StoryListSerializer
    shows (chapter total, else the EPUB/PDF estimate), read from the
    denormalized columns. NULL when the length is unknown."""
    if "_minutes" in queryset.query.annotations:
        return queryset
    return queryset.annotate(
        _minutes=Coalesce(
            NullIf(F("cached_chapter_reading_minutes"), 0),
            F("cached_file_reading_minutes"),
            output_field=IntegerField(),
        )
    )


def _tokens(value):
    return [token.strip() for token in (value or "").split(",") if token.strip()]


def apply_format(queryset, fmt):
    """Narrow to stories available in one format. Mirrors the card's format
    badges: audio / video / read-along / summary exist, and "read" means there
    is text to read here (chapters, or an uploaded EPUB or PDF)."""
    fmt = (fmt or "").strip().lower().replace("-", "_")
    if fmt == "listen":
        return queryset.filter(audios__isnull=False).distinct()
    if fmt == "watch":
        return queryset.filter(videos__isnull=False).distinct()
    if fmt == "read_along":
        return queryset.filter(
            Q(audios__audio_file__isnull=False)
            & ~Q(audios__audio_file__exact="")
            & Q(audios__transcript__isnull=False)
            & ~Q(audios__transcript__exact="")
        ).distinct()
    if fmt == "quick_read":
        return queryset.exclude(Q(summary__isnull=True) | Q(summary__exact=""))
    if fmt == "read":
        has_text = (
            Q(Exists(Chapter.objects.filter(story=OuterRef("pk"))))
            | (Q(pdf_file__isnull=False) & ~Q(pdf_file=""))
            | (Q(epub_file__isnull=False) & ~Q(epub_file=""))
        )
        return queryset.filter(has_text)
    return queryset

def public_story_mood_q():
    """Mood assignments a reader may see: an administrator's choice, or a
    machine suggestion someone has since confirmed. See StoryMood.is_public,
    which this mirrors at query level."""
    return Q(story_moods__source=StoryMood.SOURCE_ADMIN) | Q(story_moods__reviewed=True)


class StoryFilter(filters.FilterSet):
    status = filters.CharFilter(method="filter_status", label="Status")
    genres = filters.CharFilter(method="filter_genres", label="Genres")
    categories = filters.CharFilter(method="filter_categories", label="Categories")
    language = filters.CharFilter(method="filter_language", label="Language")
    story_type = filters.CharFilter(method="filter_story_type", label="Story type")
    country = filters.CharFilter(method="filter_country", label="Country")
    has_audio = filters.CharFilter(method="filter_has_audio", label="Has audio")
    has_video = filters.CharFilter(method="filter_has_video", label="Has video")
    has_summary = filters.CharFilter(method="filter_has_summary", label="Has summary")
    has_read_along = filters.CharFilter(method="filter_has_read_along", label="Has read-along")
    is_original = filters.CharFilter(method="filter_is_original", label="WorldStories Original")
    show_in_nepali_site = filters.CharFilter(
        method="filter_show_in_nepali_site", label="On the Nepali site"
    )
    moods = filters.CharFilter(method="filter_moods", label="Moods")
    # Library vocabulary: taxonomy by slug *or* id, one `format`, a length
    # bucket, an author, and `original` as the short form of `is_original`.
    genre = filters.CharFilter(method="filter_genre", label="Genre (slug or id)")
    category = filters.CharFilter(method="filter_category", label="Category (slug or id)")
    length = filters.CharFilter(method="filter_length", label="Length bucket")
    author = filters.CharFilter(method="filter_author", label="Author (id or slug)")
    format = filters.CharFilter(method="filter_format", label="Format")
    original = filters.CharFilter(method="filter_is_original", label="WorldStories Original")
    sort = filters.CharFilter(method="filter_sort", label="Sort")
    q = filters.CharFilter(method="filter_q", label="Query")

    def filter_status(self, queryset, name, value):
        if value.lower() == "completed":
            return queryset.filter(is_completed=True)
        elif value.lower() == "ongoing":
            return queryset.filter(is_completed=False)
        return queryset

    def filter_genres(self, queryset, name, value):
        genre_ids = [genre.strip() for genre in value.split(",")]
        return queryset.filter(genres__id__in=genre_ids).distinct()

    def filter_categories(self, queryset, name, value):
        category_ids = [category.strip() for category in value.split(",")]
        return queryset.filter(categories__id__in=category_ids).distinct()

    def _taxonomy(self, queryset, field, value):
        tokens = _tokens(value)
        ids = [token for token in tokens if token.isdigit()]
        slugs = [token for token in tokens if not token.isdigit()]
        if not ids and not slugs:
            return queryset
        return queryset.filter(
            Q(**{f"{field}__id__in": ids}) | Q(**{f"{field}__slug__in": slugs})
        ).distinct()

    def filter_genre(self, queryset, name, value):
        return self._taxonomy(queryset, "genres", value)

    def filter_category(self, queryset, name, value):
        return self._taxonomy(queryset, "categories", value)

    def filter_length(self, queryset, name, value):
        bounds = LENGTH_BUCKETS.get((value or "").strip().lower())
        if bounds is None:
            return queryset
        queryset = with_reading_minutes(queryset)
        low, high = bounds
        if low is not None:
            queryset = queryset.filter(_minutes__gte=low)
        if high is not None:
            queryset = queryset.filter(_minutes__lt=high)
        return queryset

    def filter_author(self, queryset, name, value):
        value = (value or "").strip()
        if not value:
            return queryset
        if value.isdigit():
            return queryset.filter(author_id=int(value))
        # Authors have no slug column, so a slug is matched against the slugified name.
        ids = [pk for pk, author_name in Author.objects.values_list("id", "name") if slugify(author_name) == value.lower()]
        return queryset.filter(author_id__in=ids)

    def filter_format(self, queryset, name, value):
        return apply_format(queryset, value)

    def filter_show_in_nepali_site(self, queryset, name, value):
        """Curated opt-in for the Nepali companion site.

        Separate from `language` on purpose — being written in Nepali and
        belonging on that site are different editorial questions. Accepts only
        "true"/"false"; anything else is ignored rather than treated as false,
        so a typo cannot silently empty the catalogue.
        """
        normalised = (value or "").strip().lower()
        if normalised in {"true", "1"}:
            return queryset.filter(show_in_nepali_site=True)
        if normalised in {"false", "0"}:
            return queryset.filter(show_in_nepali_site=False)
        return queryset

    def filter_moods(self, queryset, name, value):
        """Filter by mood slug, ignoring assignments nobody has reviewed.

        An AI suggestion is stored but is not a fact about the story until an
        administrator has confirmed it (§8.5) — so it must not steer what a
        reader is shown. `public_story_mood_q()` is the single definition of
        that rule; do not re-express it inline anywhere.
        """
        slugs = [slug.strip() for slug in value.split(",") if slug.strip()]
        if not slugs:
            return queryset
        return queryset.filter(
            public_story_mood_q(), story_moods__mood__slug__in=slugs
        ).distinct()

    def filter_language(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(language=value)

    def filter_story_type(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(story_type_id=value)

    def filter_country(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(country=value.upper())

    def filter_has_audio(self, queryset, name, value):
        if value.lower() == "true":
            return queryset.filter(audios__isnull=False).distinct()
        if value.lower() == "false":
            return queryset.filter(audios__isnull=True)
        return queryset

    def filter_has_video(self, queryset, name, value):
        if value.lower() == "true":
            return queryset.filter(videos__isnull=False).distinct()
        if value.lower() == "false":
            return queryset.filter(videos__isnull=True)
        return queryset

    def filter_has_summary(self, queryset, name, value):
        no_summary = Q(summary__isnull=True) | Q(summary__exact="")
        if value.lower() == "true":
            return queryset.exclude(no_summary)
        if value.lower() == "false":
            return queryset.filter(no_summary)
        return queryset

    def filter_has_read_along(self, queryset, name, value):
        """Approximates AudioSerializer.get_read_along_available (audio_file
        present + transcript has real content) at the DB level: a plain
        emptiness check rather than the serializer's rich-text-content parse,
        so a transcript holding only empty CKEditor markup (e.g. "<p></p>")
        would count here but not there. Close enough for list filtering;
        the per-audio value returned to the client still comes from the
        serializer, which is where read-along availability actually gets
        enforced.

        The four conditions are combined into one Q so Django joins to a
        single `audios` row per check (an existence test: "some audio has
        both a file and a transcript") — chaining separate filter()/exclude()
        calls here would instead let each condition match a *different* audio
        row, which is not what "has a read-along track" means.
        """
        has_read_along_q = (
            Q(audios__audio_file__isnull=False)
            & ~Q(audios__audio_file__exact="")
            & Q(audios__transcript__isnull=False)
            & ~Q(audios__transcript__exact="")
        )
        if value.lower() == "true":
            return queryset.filter(has_read_along_q).distinct()
        if value.lower() == "false":
            return queryset.exclude(has_read_along_q).distinct()
        return queryset

    def filter_is_original(self, queryset, name, value):
        if value.lower() == "true":
            return queryset.filter(is_original=True)
        if value.lower() == "false":
            return queryset.filter(is_original=False)
        return queryset

    def filter_sort(self, queryset, name, value):
        if value.lower() in ("recent", "newest"):
            return queryset.order_by("-site_published_date")
        elif value.lower() == "title":
            return queryset.order_by(Lower("title"), "id")
        elif value.lower() == "shortest":
            # Unknown lengths last: "no estimate" is not "shortest".
            return with_reading_minutes(queryset).order_by(F("_minutes").asc(nulls_last=True), "id")
        # Watch page: newest video first, regardless of when the story itself
        # was published. A subquery rather than Max("videos__created_at") so it
        # doesn't interact with joins added by other filters (has_video, genres…).
        elif value.lower() == "video_recent":
            latest_video_at = (
                Video.objects.filter(story=OuterRef("pk"))
                .order_by("-created_at")
                .values("created_at")[:1]
            )
            return queryset.annotate(latest_video_at=Subquery(latest_video_at)).order_by(
                F("latest_video_at").desc(nulls_last=True), "-id"
            )
        # Was ordering oldest-first (a leftover TODO), so "Popular" showed the
        # oldest stories. Most-viewed first, ties by rating then newest.
        elif value.lower() == "popular":
            return queryset.order_by("-views", "-rating", "-id")
        elif value.lower() in ("rating", "top_rated"):
            return queryset.order_by("-rating", "-views", "-id")
        elif value.lower() == "views":
            return queryset.order_by("-views")
        return queryset

    def filter_q(self, queryset, name, value):
        query = (value or "").strip()
        if not query:
            return queryset
        return queryset.filter(
            Q(title__icontains=query)
            | Q(about__icontains=query)
            | Q(author__name__icontains=query)
            | Q(genres__name__icontains=query)
            | Q(categories__name__icontains=query)
            | Q(tags__name__icontains=query)
        ).distinct()

    class Meta:
        model = Story
        fields = ["status", "genres", "categories", "language", "story_type", "country", "has_audio", "has_video", "has_summary", "has_read_along", "is_original", "sort", "q"]
