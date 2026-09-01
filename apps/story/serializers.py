import json

from rest_framework import serializers
from django.core.files.base import ContentFile
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import QueryDict
from django.utils.text import slugify
from django.urls import reverse
from datetime import date
from core.libs.images import get_cover_image_url
from .models import (
    Audio,
    AudioTranscriptCue,
    Video,
    Story,
    Genre,
    Category,
    Chapter,
    Tag,
    Theme,
    Author,
    Review,
    Submission,
    EpubImportJob,
    BookFetchJob,
    PromptSettings,
    Blog,
    StoryQueue,
    StoryType,
    published_story_q,
    with_preferred_translation_only,
)
from . import reading_time
from .audio_processing import normalize_uploaded_audio
from .youtube import parse_youtube_id, parse_duration_seconds
from .excerpts import excerpt_at_query
from .rich_text import rich_text_has_content, sanitize_reader_html

CARD_COVER_SIZE = "480x640"
LARGE_COVER_SIZE = "900x1200"
BLOG_COVER_SIZE = "1200x630"


class GenreSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()

    class Meta:
        model = Genre
        fields = ["id", "name", "slug", "description", "stories_count"]


class GenreDetailSerializer(GenreSerializer):
    stories = serializers.SerializerMethodField()

    def get_stories(self, obj):
        stories = with_preferred_translation_only(
            obj.stories.published().select_related("author").order_by("-site_published_date", "-id")
        )
        return StoryListSerializer(stories, many=True, context=self.context).data

    class Meta(GenreSerializer.Meta):
        fields = GenreSerializer.Meta.fields + ["stories"]


class CategorySerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "description", "stories_count"]


class CategoryDetailSerializer(CategorySerializer):
    stories = serializers.SerializerMethodField()

    def get_stories(self, obj):
        stories = with_preferred_translation_only(
            obj.stories.published().select_related("author").order_by("-site_published_date", "-id")
        )
        return StoryListSerializer(stories, many=True, context=self.context).data

    class Meta(CategorySerializer.Meta):
        fields = CategorySerializer.Meta.fields + ["stories"]


class TagSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()

    class Meta:
        model = Tag
        fields = ["id", "name", "slug", "description", "stories_count"]


class TagDetailSerializer(TagSerializer):
    stories = serializers.SerializerMethodField()

    def get_stories(self, obj):
        stories = with_preferred_translation_only(
            obj.stories.published().select_related("author").order_by("-site_published_date", "-id")
        )
        return StoryListSerializer(stories, many=True, context=self.context).data

    class Meta(TagSerializer.Meta):
        fields = TagSerializer.Meta.fields + ["stories"]


class ThemeSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()

    class Meta:
        model = Theme
        fields = ["id", "name", "slug", "description", "stories_count"]


class ThemeDetailSerializer(ThemeSerializer):
    stories = serializers.SerializerMethodField()

    def get_stories(self, obj):
        stories = with_preferred_translation_only(
            obj.stories.published().select_related("author").order_by("-site_published_date", "-id")
        )
        return StoryListSerializer(stories, many=True, context=self.context).data

    class Meta(ThemeSerializer.Meta):
        fields = ThemeSerializer.Meta.fields + ["stories"]


class StoryTypeSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()

    class Meta:
        model = StoryType
        fields = ["id", "name", "stories_count"]


class AuthorSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()

    class Meta:
        model = Author
        fields = ["id", "name", "bio", "image", "stories_count"]


class StoryUserSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    username = serializers.CharField()
    display_name = serializers.CharField(allow_null=True)


class AdminAuthorSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField(read_only=True)

    def get_stories_count(self, obj):
        return obj.stories.count()

    class Meta:
        model = Author
        fields = ["id", "name", "bio", "image", "stories_count"]


class AdminGenreSerializer(serializers.ModelSerializer):
    # slug is read-only here — the admin management panel's create/update
    # form is name-only (matching AdminCategorySerializer's existing
    # behavior), the viewset's perform_create generates it, and it's never
    # regenerated on rename so /genre/<slug> URLs already out there don't break.
    stories_count = serializers.SerializerMethodField(read_only=True)

    def get_stories_count(self, obj):
        return obj.stories.count()

    class Meta:
        model = Genre
        fields = ["id", "name", "slug", "stories_count"]
        read_only_fields = ["slug"]


class AdminTagSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField(read_only=True)

    def get_stories_count(self, obj):
        return obj.stories.count()

    class Meta:
        model = Tag
        fields = ["id", "name", "slug", "stories_count"]
        read_only_fields = ["slug"]


class AdminThemeSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField(read_only=True)

    def get_stories_count(self, obj):
        return obj.stories.count()

    class Meta:
        model = Theme
        fields = ["id", "name", "slug", "stories_count"]
        read_only_fields = ["slug"]


class AdminCategorySerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField(read_only=True)

    def get_stories_count(self, obj):
        return obj.stories.count()

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "stories_count"]
        read_only_fields = ["slug"]


class AdminStoryTypeSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField(read_only=True)

    def get_stories_count(self, obj):
        return obj.stories.count()

    class Meta:
        model = StoryType
        fields = ["id", "name", "stories_count"]


class StoryListSerializer(serializers.ModelSerializer):
    genres = serializers.SerializerMethodField()
    categories = serializers.SerializerMethodField()
    author = serializers.SerializerMethodField()
    story_type = serializers.CharField(source="story_type.name", read_only=True)
    summary_reading_minutes = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    is_favorite = serializers.SerializerMethodField()
    favorites_count = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()

    def get_genres(self, obj):
        return list(obj.genres.values_list("name", flat=True)[:2])

    def get_categories(self, obj):
        return list(obj.categories.values_list("name", flat=True)[:2])

    # Name only (not the full nested author object list responses use
    # elsewhere) — just enough for cards/fallback covers to credit the
    # author without bloating list payloads. Pair with select_related("author")
    # on the queryset wherever this serializer is used, or this N+1s per row.
    def get_author(self, obj):
        return obj.author.name if obj.author_id else None

    # Availability + estimate only — never the full summary body — so the
    # homepage Quick Read section (and any other list view) can show "3 min
    # read" and link to /quick-read/:slug without shipping the whole summary
    # HTML in every list response. The full text is only in StoryDetailSerializer.
    def get_summary_reading_minutes(self, obj):
        return reading_time.summary_reading_minutes(obj.summary) if obj.summary else None

    def get_reviews_count(self, obj):
        return obj.reviews.count()

    def get_is_favorite(self, obj):
        request = self.context.get("request")
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return obj.favorites.filter(user=request.user).exists()

    def get_favorites_count(self, obj):
        return obj.favorites.count()

    cover_image_size = CARD_COVER_SIZE

    def get_cover_image(self, obj):
        request = self.context.get("request")
        return get_cover_image_url(obj.cover_image_file, obj.cover_image, request, size=self.cover_image_size)

    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "story_type",
            "language",
            "site_published_date",
            "cover_image",
            "rating",
            "views",
            "has_audio",
            "has_video",
            "genres",
            "categories",
            "author",
            "summary_reading_minutes",
            "reviews_count",
            "is_favorite",
            "favorites_count",
            "is_original",
        ]


class FeaturedStorySerializer(StoryListSerializer):
    cover_image_size = LARGE_COVER_SIZE

    class Meta(StoryListSerializer.Meta):
        fields = StoryListSerializer.Meta.fields + ["about"]


class ChapterSearchResultSerializer(serializers.Serializer):
    """One chapter search hit — a lightweight, purpose-built shape (not the
    full ChapterSerializer, which is for the reader/admin and carries the
    entire chapter body). context["query"] (the search term) is required
    for the excerpt to be centered on the actual match."""

    story_slug = serializers.CharField(source="story.slug")
    story_title = serializers.CharField(source="story.title")
    story_cover_image = serializers.SerializerMethodField()
    chapter_slug = serializers.CharField(source="slug")
    chapter_title = serializers.CharField(source="title")
    excerpt = serializers.SerializerMethodField()

    def get_story_cover_image(self, obj):
        request = self.context.get("request")
        return get_cover_image_url(obj.story.cover_image_file, obj.story.cover_image, request, size=CARD_COVER_SIZE)

    def get_excerpt(self, obj):
        return excerpt_at_query(obj, self.context.get("query", ""))


class AuthorDetailSerializer(AuthorSerializer):
    stories = serializers.SerializerMethodField()

    def get_stories(self, obj):
        stories = obj.stories.published().select_related("author").order_by("-site_published_date", "-id")
        stories = with_preferred_translation_only(stories)
        return StoryListSerializer(stories, many=True, context=self.context).data

    class Meta(AuthorSerializer.Meta):
        fields = AuthorSerializer.Meta.fields + ["stories"]


class ChapterListSerializer(serializers.ModelSerializer):
    download_size_bytes = serializers.SerializerMethodField()

    def get_download_size_bytes(self, obj):
        payload = {"id": str(obj.id), "title": obj.title, "order": obj.order, "content": obj.content, "slug": obj.slug}
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    class Meta:
        model = Chapter
        fields = ["id", "title", "order", "slug", "download_size_bytes"]


class ChapterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chapter
        fields = ["id", "title", "order", "content", "slug"]


# Timed cues may legitimately run a little past the probed audio duration
# (encoder padding, a trailing "[music]" cue). Anything beyond this margin is
# treated as a broken import and rejected.
CUE_DURATION_GRACE_MS = 2000


def validate_cue_sequence(cues, *, audio_duration_ms=None):
    """Validate an ordered list of cue dicts (each with order/start_ms/end_ms).

    Individual field checks (end > start, non-empty text) live on
    ``AudioTranscriptCueSerializer``; this covers the cross-cue invariants:
    strictly ascending unique order, no unexpected overlaps, and cues not
    running substantially beyond the known audio duration.
    """
    if not cues:
        return

    orders = [cue["order"] for cue in cues]
    if len(set(orders)) != len(orders):
        raise serializers.ValidationError("Cue order values must be unique.")

    previous_order = None
    previous_end = None
    for cue in cues:
        if previous_order is not None and cue["order"] <= previous_order:
            raise serializers.ValidationError("Cues must be provided in ascending order.")
        if previous_end is not None and cue["start_ms"] < previous_end:
            raise serializers.ValidationError(
                f"Cue {cue['order']} overlaps the previous cue."
            )
        previous_order = cue["order"]
        previous_end = cue["end_ms"]

    if audio_duration_ms is not None:
        last_end = cues[-1]["end_ms"]
        if last_end > audio_duration_ms + CUE_DURATION_GRACE_MS:
            raise serializers.ValidationError(
                "Cues extend substantially beyond the audio track's duration."
            )


class AudioTranscriptCueListSerializer(serializers.ListSerializer):
    def validate(self, cues):
        validate_cue_sequence(cues, audio_duration_ms=self.context.get("audio_duration_ms"))
        return cues


class AudioTranscriptCueSerializer(serializers.ModelSerializer):
    class Meta:
        model = AudioTranscriptCue
        fields = ["order", "start_ms", "end_ms", "text"]
        list_serializer_class = AudioTranscriptCueListSerializer

    def validate_text(self, value):
        if not value.strip():
            raise serializers.ValidationError("Cue text cannot be empty.")
        return value

    def validate(self, attrs):
        start_ms = attrs.get("start_ms", getattr(self.instance, "start_ms", None))
        end_ms = attrs.get("end_ms", getattr(self.instance, "end_ms", None))
        if start_ms is not None and end_ms is not None and end_ms <= start_ms:
            raise serializers.ValidationError({"end_ms": "End time must be after start time."})
        return attrs


def _resolve_transcript_synchronized(obj):
    """Story-detail querysets annotate ``_has_timed_cues`` to avoid an N+1;
    other call sites fall back to a direct existence check."""
    if not rich_text_has_content(obj.transcript):
        return False
    cached = getattr(obj, "_has_timed_cues", None)
    return cached if cached is not None else obj.transcript_cues.exists()


class AudioSerializer(serializers.ModelSerializer):
    download_size_bytes = serializers.IntegerField(source="file_size_bytes", read_only=True)
    has_transcript = serializers.SerializerMethodField()
    read_along_available = serializers.SerializerMethodField()
    transcript_synchronized = serializers.SerializerMethodField()

    def get_has_transcript(self, obj):
        return rich_text_has_content(obj.transcript)

    def get_read_along_available(self, obj):
        return bool(obj.audio_file) and self.get_has_transcript(obj)

    def get_transcript_synchronized(self, obj):
        return _resolve_transcript_synchronized(obj)

    class Meta:
        model = Audio
        fields = [
            "id", "title", "slug", "audio_file", "order", "download_size_bytes",
            "has_transcript", "read_along_available", "transcript_synchronized",
        ]


class AudioListSerializer(serializers.ModelSerializer):
    has_transcript = serializers.SerializerMethodField()
    read_along_available = serializers.SerializerMethodField()
    transcript_synchronized = serializers.SerializerMethodField()

    def get_has_transcript(self, obj):
        return rich_text_has_content(obj.transcript)

    def get_read_along_available(self, obj):
        return bool(obj.audio_file) and self.get_has_transcript(obj)

    def get_transcript_synchronized(self, obj):
        return _resolve_transcript_synchronized(obj)

    class Meta:
        model = Audio
        fields = [
            "id", "title", "slug", "order", "has_transcript",
            "read_along_available", "transcript_synchronized",
        ]


class ReadAlongSerializer(serializers.BaseSerializer):
    """Dedicated representation; complete transcript HTML never enters normal story payloads."""

    def to_representation(self, audio):
        request = self.context.get("request")
        story = self.context["story"]

        audio_file_url = None
        if audio.audio_file:
            try:
                audio_file_url = audio.audio_file.url
            except (AttributeError, ValueError):
                audio_file_url = None
            if audio_file_url and request:
                audio_file_url = request.build_absolute_uri(audio_file_url)

        stream_url = None
        if audio.audio_file:
            stream_path = reverse(
                "story-audio-stream",
                kwargs={"slug": story.slug, "audio_slug": audio.slug},
            )
            stream_url = request.build_absolute_uri(stream_path) if request else stream_path
        has_transcript = rich_text_has_content(audio.transcript)
        transcript_html = sanitize_reader_html(audio.transcript) if has_transcript else ""

        # `transcript_cues` is prefetched (ordered) by the read_along view.
        cue_payload = [
            {
                "id": cue.id,
                "start_seconds": round(cue.start_ms / 1000, 3),
                "end_seconds": round(cue.end_ms / 1000, 3),
                "text": cue.text,
            }
            for cue in audio.transcript_cues.all()
        ]
        synchronized = has_transcript and bool(cue_payload)

        return {
            "story": {
                "id": story.id,
                "title": story.title,
                "slug": story.slug,
                "language": story.language,
                "story_type": story.story_type.name,
                "cover_image": get_cover_image_url(
                    story.cover_image_file, story.cover_image, request, size=LARGE_COVER_SIZE
                ),
                "author": (
                    {"id": story.author.id, "name": story.author.name}
                    if story.author_id
                    else None
                ),
            },
            "audio": {
                "id": audio.id,
                "title": audio.title,
                "slug": audio.slug,
                "order": audio.order,
                "audio_file": audio_file_url,
                "stream_url": stream_url,
                "duration_seconds": audio.duration_seconds,
                "download_size_bytes": audio.file_size_bytes,
                "has_transcript": has_transcript,
                "read_along_available": bool(audio.audio_file) and has_transcript,
                "transcript_synchronized": synchronized,
            },
            "transcript": {
                "html": transcript_html,
                "state": (
                    "empty"
                    if not has_transcript
                    else "synchronized" if synchronized else "unsynchronized"
                ),
                "synchronized": synchronized,
                "cues": cue_payload,
                "default_offset_seconds": round(audio.read_along_offset_ms / 1000, 3),
            },
            "navigation": {
                "previous_audio_slug": self.context.get("previous_audio_slug"),
                "next_audio_slug": self.context.get("next_audio_slug"),
            },
        }


class VideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Video
        fields = ["id", "title", "slug", "youtube_id", "order", "duration_seconds"]


def similar_stories_candidates(story):
    """Shared 'similar to `story`' candidate query — genre/author/
    story_type/language overlap, excluding its own translation group. Used
    by StoryDetailSerializer.get_similar_stories (generic, no
    personalization) and apps.users.recommendations.recommend_because_finished
    (personalized, re-ranked toward a specific user's taste)."""
    genre_ids = list(story.genres.values_list("id", flat=True))
    matching = Q(story_type=story.story_type) | Q(language=story.language)
    if genre_ids:
        matching |= Q(genres__id__in=genre_ids)
    if story.author_id:
        matching |= Q(author_id=story.author_id)

    candidates = Story.objects.published().select_related("author").filter(matching).exclude(
        translation_group=story.translation_group
    )
    candidates = with_preferred_translation_only(candidates).annotate(
        shared_genres=Count(
            "genres",
            filter=Q(genres__id__in=genre_ids),
            distinct=True,
        ),
        same_author=Case(
            *(
                [When(author_id=story.author_id, then=Value(1))]
                if story.author_id
                else []
            ),
            default=Value(0),
            output_field=IntegerField(),
        ),
        same_story_type=Case(
            When(story_type=story.story_type, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
        same_language=Case(
            When(language=story.language, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    )
    return candidates, genre_ids


class StoryDetailSerializer(serializers.ModelSerializer):
    cover_image = serializers.SerializerMethodField()
    pdf_file = serializers.SerializerMethodField()
    epub_file = serializers.SerializerMethodField()
    story_type = serializers.CharField(source="story_type.name", read_only=True)
    genres = GenreSerializer(many=True, read_only=True)
    categories = CategorySerializer(many=True, read_only=True)
    author = AuthorSerializer(read_only=True)
    submitted_by = serializers.SerializerMethodField()
    chapter_count = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    is_favorite = serializers.SerializerMethodField()
    favorites_count = serializers.SerializerMethodField()
    chapters = ChapterListSerializer(many=True, read_only=True)
    audios = AudioSerializer(many=True, read_only=True)
    videos = VideoSerializer(many=True, read_only=True)
    translations = serializers.SerializerMethodField()
    reading_time_minutes = serializers.SerializerMethodField()
    listening_time_minutes = serializers.SerializerMethodField()
    watch_time_minutes = serializers.SerializerMethodField()
    published_date_label = serializers.SerializerMethodField()
    pdf_size_bytes = serializers.SerializerMethodField()
    epub_size_bytes = serializers.SerializerMethodField()
    similar_stories = serializers.SerializerMethodField()

    @staticmethod
    def _file_size(file_field):
        try:
            return file_field.size if file_field else 0
        except Exception:
            return 0

    def get_pdf_size_bytes(self, obj):
        return self._file_size(obj.pdf_file)

    def get_epub_size_bytes(self, obj):
        return self._file_size(obj.epub_file)

    def get_chapter_count(self, obj):
        return obj.chapters.count()

    def get_published_date_label(self, obj):
        original = obj.original_published_date_display()
        if original:
            return original
        if obj.site_published_date:
            return obj.site_published_date.strftime("%B %-d, %Y")
        return None

    def get_reading_time_minutes(self, obj):
        return reading_time.story_reading_minutes(obj)

    def get_listening_time_minutes(self, obj):
        return reading_time.story_listening_minutes(obj.audios.all())

    def get_watch_time_minutes(self, obj):
        return reading_time.story_watch_minutes(obj.videos.all())

    def get_translations(self, obj):
        siblings = (
            Story.objects.filter(published_story_q(), translation_group=obj.translation_group)
            .exclude(pk=obj.pk)
            .only("id", "slug", "language", "title")
            .order_by("language")
        )
        return [
            {"id": sibling.id, "slug": sibling.slug, "language": sibling.language, "title": sibling.title}
            for sibling in siblings
        ]

    def get_similar_stories(self, obj):
        candidates, _ = similar_stories_candidates(obj)
        candidates = candidates.order_by(
            "-shared_genres",
            "-same_author",
            "-same_story_type",
            "-same_language",
            "-rating",
            "-views",
            "-id",
        )[:6]

        return StoryListSerializer(candidates, many=True, context=self.context).data
    
    def get_reviews_count(self, obj):
        return obj.reviews.count()

    def get_is_favorite(self, obj):
        request = self.context.get("request")
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return obj.favorites.filter(user=request.user).exists()

    def get_favorites_count(self, obj):
        return obj.favorites.count()

    def get_cover_image(self, obj):
        request = self.context.get("request")
        return get_cover_image_url(obj.cover_image_file, obj.cover_image, request, size=LARGE_COVER_SIZE)

    def get_pdf_file(self, obj):
        if not obj.pdf_file:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.pdf_file.url)
        return obj.pdf_file.url

    def get_epub_file(self, obj):
        if not obj.epub_file:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.epub_file.url)
        return obj.epub_file.url

    def get_submitted_by(self, obj):
        if not obj.submitted_by:
            return None
        return StoryUserSerializer(obj.submitted_by).data

    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "about",
            "summary",
            "retrospective",
            "genres",
            "categories",
            "story_type",
            "language",
            "translations",
            "author",
            "submitted_by",
            "original_published_year",
            "original_published_month",
            "original_published_day",
            "site_published_date",
            "published_date_label",
            "cover_image",
            "pdf_file",
            "epub_file",
            "pdf_size_bytes",
            "epub_size_bytes",
            "is_completed",
            "is_original",
            "tags",
            "themes",
            "rating",
            "views",
            "reviews_count",
            "is_favorite",
            "favorites_count",
            "chapter_count",
            "chapters",
            "audios",
            "videos",
            "has_audio",
            "has_video",
            "reading_time_minutes",
            "listening_time_minutes",
            "watch_time_minutes",
            "similar_stories",
        ]


class ReviewUserSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    username = serializers.CharField()


class ReviewSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()

    def get_user(self, obj):
        return ReviewUserSerializer(obj.user).data

    class Meta:
        model = Review
        fields = ["id", "user", "rating", "comment", "created_at", "updated_at"]


class ReviewWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ["rating", "comment"]


class SubmissionSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    # Wire format stays a plain name string (not an id) — the public
    # submission form only ever offers a live-fetched list of existing
    # StoryType names to pick from, never creates a new one, so there's no
    # need to expose ids here at all.
    story_type = serializers.SlugRelatedField(slug_field="name", queryset=StoryType.objects.all())
    genres = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), many=True
    )
    cover_image = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )

    class Meta:
        model = Submission
        fields = [
            "id",
            "title",
            "about",
            "content",
            "story_type",
            "language",
            "genres",
            "cover_image",
            "cover_image_file",
            "notes",
            "pdf_file",
            "epub_file",
            "status",
            "reviewer_notes",
            "published_story",
            "created_at",
            "updated_at",
            "user_email",
        ]
        read_only_fields = [
            "status",
            "reviewer_notes",
            "published_story",
            "created_at",
            "updated_at",
            "user_email",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.cover_image_file:
            request = self.context.get("request")
            if request:
                data["cover_image"] = request.build_absolute_uri(instance.cover_image_file.url)
            else:
                data["cover_image"] = instance.cover_image_file.url
        else:
            data["cover_image"] = instance.cover_image or ""
        return data

    def validate_pdf_file(self, value):
        if value and not value.name.lower().endswith(".pdf"):
            raise serializers.ValidationError("Only PDF files are allowed.")
        return value

    def validate_epub_file(self, value):
        if value and not value.name.lower().endswith(".epub"):
            raise serializers.ValidationError("Only EPUB files are allowed.")
        return value

    def create(self, validated_data):
        genres = validated_data.pop("genres", [])
        submission = Submission.objects.create(user=self.context["request"].user, **validated_data)
        submission.genres.set(genres)
        return submission


class SubmissionListSerializer(serializers.ModelSerializer):
    genres = GenreSerializer(many=True, read_only=True)
    cover_image = serializers.SerializerMethodField()
    story_type = serializers.CharField(source="story_type.name", read_only=True)

    class Meta:
        model = Submission
        fields = [
            "id",
            "title",
            "story_type",
            "language",
            "genres",
            "cover_image",
            "status",
            "reviewer_notes",
            "published_story",
            "reviewed_at",
            "created_at",
            "updated_at",
        ]

    def get_cover_image(self, obj):
        if obj.cover_image_file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.cover_image_file.url)
            return obj.cover_image_file.url
        return obj.cover_image or ""


class StoryAdminSerializer(serializers.ModelSerializer):
    # Sentinel distinguishing "leave cached_file_reading_minutes untouched"
    # from a legitimate computed value of None (no words found, format not
    # parseable, etc.) — see _file_reading_minutes_for_upload.
    _CACHE_UNCHANGED = object()

    author = serializers.PrimaryKeyRelatedField(
        queryset=Author.objects.all(), required=False, allow_null=True
    )
    story_type = serializers.PrimaryKeyRelatedField(queryset=StoryType.objects.all())
    genres = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), many=True, required=False
    )
    categories = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), many=True, required=False
    )
    tags = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(), many=True, required=False
    )
    cover_image = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    remove_cover_image_file = serializers.BooleanField(required=False, write_only=True)
    remove_pdf_file = serializers.BooleanField(required=False, write_only=True)
    remove_epub_file = serializers.BooleanField(required=False, write_only=True)
    cover_image_url = serializers.SerializerMethodField(read_only=True)
    pdf_file_url = serializers.SerializerMethodField(read_only=True)
    epub_file_url = serializers.SerializerMethodField(read_only=True)
    submitted_by = serializers.SerializerMethodField(read_only=True)
    source = serializers.SerializerMethodField(read_only=True)
    translations = serializers.SerializerMethodField(read_only=True)
    published_date_label = serializers.SerializerMethodField(read_only=True)
    chapter_count = serializers.SerializerMethodField(read_only=True)
    audio_count = serializers.SerializerMethodField(read_only=True)
    video_count = serializers.SerializerMethodField(read_only=True)

    def get_chapter_count(self, obj):
        return obj.chapters.count()

    def get_audio_count(self, obj):
        return obj.audios.count()

    def get_video_count(self, obj):
        return obj.videos.count()

    def get_published_date_label(self, obj):
        original = obj.original_published_date_display()
        if original:
            return original
        if obj.site_published_date:
            return obj.site_published_date.strftime("%B %-d, %Y")
        return None

    def get_translations(self, obj):
        siblings = (
            Story.objects.filter(translation_group=obj.translation_group)
            .exclude(pk=obj.pk)
            .only("id", "slug", "language", "title")
            .order_by("language")
        )
        return [
            {"id": sibling.id, "slug": sibling.slug, "language": sibling.language, "title": sibling.title}
            for sibling in siblings
        ]

    # The admin form submits these as multipart FormData, where "clear this
    # optional value" naturally comes through as an empty string rather than
    # omitting the key — but DRF's Date/DateTime/IntegerField only treats an
    # actual None as "no value" and rejects "" as an invalid format.
    # Normalizing empty strings to None here (before field validation) lets
    # clearing any of these fields actually work instead of erroring.
    CLEARABLE_DATE_FIELDS = (
        "original_published_year",
        "original_published_month",
        "original_published_day",
        "site_published_date",
        "publish_at",
    )

    def to_internal_value(self, data):
        if isinstance(data, QueryDict):
            # QueryDict.copy() performs a deep copy. On multipart requests that
            # attempts to pickle open UploadedFile streams, which raises on
            # Python 3.14. Preserve repeated form fields with a shallow copy.
            normalized = QueryDict("", mutable=True, encoding=data.encoding)
            for key in data:
                normalized.setlist(key, list(data.getlist(key)))
            data = normalized
        elif hasattr(data, "copy"):
            data = data.copy()

        if hasattr(data, "get"):
            for field_name in self.CLEARABLE_DATE_FIELDS:
                if data.get(field_name) == "":
                    data[field_name] = None
        return super().to_internal_value(data)

    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "about",
            "summary",
            "summary_status",
            "summary_source",
            "summary_confident",
            "summary_confidence_note",
            "summary_error",
            "retrospective",
            "retrospective_status",
            "retrospective_source",
            "retrospective_confident",
            "retrospective_confidence_note",
            "retrospective_error",
            "story_type",
            "language",
            "country",
            "translations",
            "author",
            "submitted_by",
            "original_published_year",
            "original_published_month",
            "original_published_day",
            "published_date_label",
            "site_published_date",
            "is_published",
            "publish_at",
            "chapter_count",
            "audio_count",
            "video_count",
            "cover_image",
            "cover_image_file",
            "remove_cover_image_file",
            "cover_image_url",
            "pdf_file",
            "remove_pdf_file",
            "pdf_file_url",
            "epub_file",
            "remove_epub_file",
            "epub_file_url",
            "cached_file_reading_minutes",
            "is_completed",
            "is_original",
            "genres",
            "categories",
            "tags",
            "themes",
            "rating",
            "views",
            "source",
        ]
        read_only_fields = [
            "rating", "views", "cached_file_reading_minutes",
            "summary_status", "summary_source", "summary_confident", "summary_confidence_note", "summary_error",
            "retrospective_status", "retrospective_source", "retrospective_confident",
            "retrospective_confidence_note", "retrospective_error",
        ]
        # slug isn't required at the request level — validate() below auto-generates
        # it from the title when omitted, but that only runs after field-level
        # validation, so `slug` must not be marked required there.
        extra_kwargs = {"slug": {"required": False}}

    def get_submitted_by(self, obj):
        if not obj.submitted_by:
            return None
        return StoryUserSerializer(obj.submitted_by).data

    def get_source(self, obj):
        try:
            submission = obj.submission
        except Submission.DoesNotExist:
            return "admin"
        return "submission" if submission else "admin"

    def _build_unique_slug(self, title: str, instance=None) -> str:
        base_slug = slugify(title) or "story"
        slug = base_slug
        suffix = 2
        queryset = Story.objects.all()
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        while queryset.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    def validate_pdf_file(self, value):
        if value and not value.name.lower().endswith(".pdf"):
            raise serializers.ValidationError("Only PDF files are allowed.")
        return value

    def validate_epub_file(self, value):
        if value and not value.name.lower().endswith(".epub"):
            raise serializers.ValidationError("Only EPUB files are allowed.")
        return value

    def validate(self, attrs):
        title = attrs.get("title") or getattr(self.instance, "title", "")
        slug = attrs.get("slug")
        if not slug and title:
            attrs["slug"] = self._build_unique_slug(title, self.instance)

        # Only meaningful in combination — a day without a month, or a month
        # without a year, can't be formatted into anything sensible.
        year = attrs.get("original_published_year", getattr(self.instance, "original_published_year", None))
        month = attrs.get("original_published_month", getattr(self.instance, "original_published_month", None))
        day = attrs.get("original_published_day", getattr(self.instance, "original_published_day", None))
        if month and not year:
            raise serializers.ValidationError({"original_published_month": "Requires the year to also be set."})
        if day and not month:
            raise serializers.ValidationError({"original_published_day": "Requires the month to also be set."})
        if year and month and day:
            try:
                date(year, month, day)
            except ValueError:
                raise serializers.ValidationError(
                    {"original_published_day": "This is not a valid calendar date."}
                )

        return attrs

    def get_cover_image_url(self, obj):
        if obj.cover_image_file:
            request = self.context.get("request")
            return request.build_absolute_uri(obj.cover_image_file.url) if request else obj.cover_image_file.url
        return obj.cover_image or ""

    def get_pdf_file_url(self, obj):
        if not obj.pdf_file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.pdf_file.url) if request else obj.pdf_file.url

    def get_epub_file_url(self, obj):
        if not obj.epub_file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.epub_file.url) if request else obj.epub_file.url

    # Computes the epub/pdf-derived reading-time estimate from whichever file
    # was *just uploaded this request* — never from a file already sitting in
    # remote storage, which would mean fetching it back over the network (see
    # reading_time.py's module docstring). new_epub/new_pdf are the local,
    # not-yet-saved upload objects from validated_data.
    def _file_reading_minutes_for_upload(self, new_epub, new_pdf, epub_survives, pdf_survives):
        if new_epub is not None:
            raw_bytes = reading_time.read_local_upload_bytes(new_epub)
            return reading_time.epub_minutes_from_bytes(raw_bytes) if raw_bytes is not None else None
        if new_pdf is not None and not epub_survives:
            raw_bytes = reading_time.read_local_upload_bytes(new_pdf)
            return reading_time.pdf_minutes_from_bytes(raw_bytes) if raw_bytes is not None else None
        if not epub_survives and not pdf_survives:
            return None
        # A file was removed/replaced but whatever's left (an unchanged epub
        # or pdf) wasn't re-uploaded this request — recomputing it would mean
        # fetching it back from remote storage, which this cache exists to
        # avoid, so the previous estimate is left in place instead.
        return self._CACHE_UNCHANGED

    def update(self, instance, validated_data):
        remove_cover_image_file = bool(validated_data.pop("remove_cover_image_file", False))
        remove_pdf_file = bool(validated_data.pop("remove_pdf_file", False))
        remove_epub_file = bool(validated_data.pop("remove_epub_file", False))

        if remove_cover_image_file and instance.cover_image_file:
            instance.cover_image_file.delete(save=False)
            instance.cover_image_file = None
        if remove_pdf_file and instance.pdf_file:
            instance.pdf_file.delete(save=False)
            instance.pdf_file = None
        if remove_epub_file and instance.epub_file:
            instance.epub_file.delete(save=False)
            instance.epub_file = None

        cached_minutes = self._file_reading_minutes_for_upload(
            validated_data.get("epub_file"),
            validated_data.get("pdf_file"),
            epub_survives=bool(instance.epub_file),
            pdf_survives=bool(instance.pdf_file),
        )
        if cached_minutes is not self._CACHE_UNCHANGED:
            validated_data["cached_file_reading_minutes"] = cached_minutes

        return super().update(instance, validated_data)

    def create(self, validated_data):
        # These flags are only meaningful for updates and are not model fields.
        validated_data.pop("remove_cover_image_file", None)
        validated_data.pop("remove_pdf_file", None)
        validated_data.pop("remove_epub_file", None)

        new_epub = validated_data.get("epub_file")
        new_pdf = validated_data.get("pdf_file")
        cached_minutes = self._file_reading_minutes_for_upload(
            new_epub, new_pdf, epub_survives=bool(new_epub), pdf_survives=bool(new_pdf)
        )
        if cached_minutes is not self._CACHE_UNCHANGED:
            validated_data["cached_file_reading_minutes"] = cached_minutes

        return super().create(validated_data)


class ChapterAdminSerializer(serializers.ModelSerializer):
    def _build_unique_slug(self, story, title: str, instance=None) -> str:
        base_slug = slugify(title) or "chapter"
        slug = base_slug
        suffix = 2
        queryset = Chapter.objects.filter(story=story)
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        while queryset.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    def validate(self, attrs):
        story = attrs.get("story") or getattr(self.instance, "story", None)
        title = attrs.get("title") or getattr(self.instance, "title", "")
        slug = attrs.get("slug")

        if story and title and (slug is None or str(slug).strip() == ""):
            attrs["slug"] = self._build_unique_slug(story, title, self.instance)
        return attrs

    class Meta:
        model = Chapter
        fields = ["id", "story", "title", "slug", "content", "order"]


class EpubImportJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = EpubImportJob
        fields = [
            "id", "story", "status", "error_message", "chapters_created",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class BookFetchJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookFetchJob
        fields = [
            "id", "requested_count", "created_count", "skipped_count", "status", "error_message",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class PromptSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromptSettings
        fields = [
            "summary_instructions", "summary_model",
            "retrospective_instructions", "retrospective_model",
            "excerpt_instructions", "excerpt_model",
            "book_fetch_instructions", "book_fetch_model",
        ]


class LinkedStorySummarySerializer(serializers.ModelSerializer):
    cover_image = serializers.SerializerMethodField()
    author = serializers.SerializerMethodField()
    story_type = serializers.CharField(source="story_type.name", read_only=True)

    def get_cover_image(self, obj):
        request = self.context.get("request")
        return get_cover_image_url(obj.cover_image_file, obj.cover_image, request, size=CARD_COVER_SIZE)

    def get_author(self, obj):
        return obj.author.name if obj.author_id else None

    class Meta:
        model = Story
        fields = ["id", "slug", "title", "cover_image", "author", "story_type", "language"]


class LinkedBlogSummarySerializer(serializers.ModelSerializer):
    cover_image = serializers.SerializerMethodField()
    published_at = serializers.DateTimeField(source="created_at", read_only=True)

    def get_cover_image(self, obj):
        request = self.context.get("request")
        return get_cover_image_url(obj.cover_image_file, None, request, size=BLOG_COVER_SIZE)

    class Meta:
        model = Blog
        fields = ["id", "slug", "title", "excerpt", "cover_image", "author_name", "published_at"]


class BlogSerializer(serializers.ModelSerializer):
    cover_image = serializers.SerializerMethodField()
    linked_stories = serializers.SerializerMethodField()
    linked_blogs = serializers.SerializerMethodField()
    published_at = serializers.DateTimeField(source="created_at", read_only=True)

    def get_cover_image(self, obj):
        request = self.context.get("request")
        return get_cover_image_url(obj.cover_image_file, None, request, size=BLOG_COVER_SIZE)

    def get_linked_stories(self, obj):
        stories = getattr(obj, "published_linked_stories", None)
        if stories is None:
            stories = obj.linked_stories.published().select_related("author", "story_type")
        return LinkedStorySummarySerializer(stories, many=True, context=self.context).data

    def get_linked_blogs(self, obj):
        blogs = getattr(obj, "published_linked_blogs", None)
        if blogs is None:
            blogs = obj.linked_blogs.published()
        return LinkedBlogSummarySerializer(blogs, many=True, context=self.context).data

    class Meta:
        model = Blog
        fields = [
            "id", "title", "slug", "excerpt", "content", "cover_image",
            "author_name", "linked_stories", "linked_blogs", "published_at", "updated_at",
        ]


class BlogAdminSerializer(serializers.ModelSerializer):
    linked_stories = serializers.PrimaryKeyRelatedField(
        queryset=Story.objects.all(), many=True, required=False
    )
    linked_blogs = serializers.PrimaryKeyRelatedField(
        queryset=Blog.objects.all(), many=True, required=False
    )
    linked_story_details = serializers.SerializerMethodField(read_only=True)
    linked_blog_details = serializers.SerializerMethodField(read_only=True)
    remove_cover_image_file = serializers.BooleanField(required=False, write_only=True)
    cover_image_url = serializers.SerializerMethodField(read_only=True)
    # Copies a story's cover image onto this blog post server-side (avoids a
    # browser-side cross-origin fetch of the R2-hosted image, which R2's
    # default bucket CORS config would likely block) — see the "use this
    # story's cover" admin-panel prompt shown when linking a story.
    copy_cover_from_story = serializers.PrimaryKeyRelatedField(
        queryset=Story.objects.all(), required=False, allow_null=True, write_only=True
    )

    CLEARABLE_DATE_FIELDS = ("publish_at",)
    CLEARABLE_MANY_FIELDS = ("linked_stories", "linked_blogs")

    def to_internal_value(self, data):
        # Same multipart-empty-string-means-"clear this field" handling as
        # StoryAdminSerializer, including the Python 3.14 QueryDict.copy()
        # pickle-on-open-UploadedFile-streams workaround.
        if isinstance(data, QueryDict):
            normalized = QueryDict("", mutable=True, encoding=data.encoding)
            for key in data:
                normalized.setlist(key, list(data.getlist(key)))
            data = normalized
        elif hasattr(data, "copy"):
            data = data.copy()

        if hasattr(data, "get"):
            for field_name in self.CLEARABLE_DATE_FIELDS:
                if data.get(field_name) == "":
                    data[field_name] = None
            if hasattr(data, "getlist") and hasattr(data, "setlist"):
                for field_name in self.CLEARABLE_MANY_FIELDS:
                    if data.getlist(field_name) == [""]:
                        data.setlist(field_name, [])
        return super().to_internal_value(data)

    def get_linked_story_details(self, obj):
        return list(obj.linked_stories.values("id", "title", "slug"))

    def get_linked_blog_details(self, obj):
        return list(obj.linked_blogs.values("id", "title", "slug"))

    def get_cover_image_url(self, obj):
        if obj.cover_image_file:
            request = self.context.get("request")
            return request.build_absolute_uri(obj.cover_image_file.url) if request else obj.cover_image_file.url
        return ""

    def _build_unique_slug(self, title: str, instance=None) -> str:
        base_slug = slugify(title) or "blog"
        slug = base_slug
        suffix = 2
        queryset = Blog.objects.all()
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        while queryset.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    def validate(self, attrs):
        title = attrs.get("title") or getattr(self.instance, "title", "")
        if not attrs.get("slug") and title:
            attrs["slug"] = self._build_unique_slug(title, self.instance)
        if self.instance and self.instance in attrs.get("linked_blogs", []):
            raise serializers.ValidationError({"linked_blogs": "A blog post cannot link to itself."})
        return attrs

    def _copy_cover_from_story(self, instance, story):
        if not story.cover_image_file:
            return
        source = story.cover_image_file
        source.open("rb")
        try:
            content = source.read()
        finally:
            source.close()
        filename = source.name.rsplit("/", 1)[-1]
        instance.cover_image_file.save(filename, ContentFile(content), save=False)

    def update(self, instance, validated_data):
        copy_cover_from_story = validated_data.pop("copy_cover_from_story", None)
        # Explicit removal/upload takes priority over a "copy from story"
        # request landing in the same payload — not reachable via the admin
        # UI today (they're presented as alternatives), but this keeps the
        # precedence unambiguous if it ever is.
        if validated_data.pop("remove_cover_image_file", False) and instance.cover_image_file:
            instance.cover_image_file.delete(save=False)
            instance.cover_image_file = None
        instance = super().update(instance, validated_data)
        if copy_cover_from_story and "cover_image_file" not in validated_data:
            self._copy_cover_from_story(instance, copy_cover_from_story)
            instance.save(update_fields=["cover_image_file"])
        return instance

    def create(self, validated_data):
        copy_cover_from_story = validated_data.pop("copy_cover_from_story", None)
        validated_data.pop("remove_cover_image_file", None)
        instance = super().create(validated_data)
        if copy_cover_from_story and "cover_image_file" not in validated_data:
            self._copy_cover_from_story(instance, copy_cover_from_story)
            instance.save(update_fields=["cover_image_file"])
        return instance

    class Meta:
        model = Blog
        fields = [
            "id", "title", "slug", "excerpt",
            "excerpt_status", "excerpt_source", "excerpt_confident", "excerpt_confidence_note", "excerpt_error",
            "content", "cover_image_file",
            "remove_cover_image_file", "copy_cover_from_story", "cover_image_url", "author_name",
            "linked_stories", "linked_story_details", "linked_blogs", "linked_blog_details",
            "is_published", "publish_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "created_at", "updated_at",
            "excerpt_status", "excerpt_source", "excerpt_confident", "excerpt_confidence_note", "excerpt_error",
        ]
        extra_kwargs = {"slug": {"required": False}}


class AudioAdminSerializer(serializers.ModelSerializer):
    transcript_synchronized = serializers.SerializerMethodField()
    cue_count = serializers.SerializerMethodField()

    def get_transcript_synchronized(self, obj):
        return _resolve_transcript_synchronized(obj)

    def get_cue_count(self, obj):
        cached = getattr(obj, "_cue_count", None)
        return cached if cached is not None else obj.transcript_cues.count()

    def _build_unique_slug(self, story, title: str, instance=None) -> str:
        base_slug = slugify(title) or "audio"
        slug = base_slug
        suffix = 2
        queryset = Audio.objects.filter(story=story)
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        while queryset.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    def validate(self, attrs):
        story = attrs.get("story") or getattr(self.instance, "story", None)
        title = attrs.get("title") or getattr(self.instance, "title", "")
        slug = attrs.get("slug")

        if story and title and (slug is None or str(slug).strip() == ""):
            attrs["slug"] = self._build_unique_slug(story, title, self.instance)
        return attrs

    def validate_audio_file(self, value):
        # Re-encodes on the way in if the source uses a legacy sample rate
        # some browsers can't decode — see audio_processing.py.
        return normalize_uploaded_audio(value) if value else value

    def _probe_duration_from_upload(self, uploaded_file):
        # Probes the file that's about to be saved, not the one that was just
        # saved — reading it back from instance.audio_file after super().create()/
        # update() would mean a live network round-trip to remote storage (see
        # reading_time.probe_audio_duration_seconds). The upload is already
        # sitting in memory/local temp storage right here, pre-save.
        raw_bytes = reading_time.read_local_upload_bytes(uploaded_file)
        if raw_bytes is None:
            return None
        return reading_time.probe_audio_duration_from_bytes(raw_bytes)

    def create(self, validated_data):
        audio_file = validated_data.get("audio_file")
        validated_data["file_size_bytes"] = getattr(audio_file, "size", 0) or 0
        duration = self._probe_duration_from_upload(audio_file)
        instance = super().create(validated_data)
        if duration is not None:
            instance.duration_seconds = duration
            instance.save(update_fields=["duration_seconds"])
        return instance

    def update(self, instance, validated_data):
        audio_file = validated_data.get("audio_file")
        duration = None
        if audio_file is not None:
            validated_data["file_size_bytes"] = getattr(audio_file, "size", 0) or 0
            duration = self._probe_duration_from_upload(audio_file)
        instance = super().update(instance, validated_data)
        if "audio_file" in validated_data and duration is not None:
            instance.duration_seconds = duration
            instance.save(update_fields=["duration_seconds"])
        return instance

    class Meta:
        model = Audio
        fields = [
            "id",
            "story",
            "title",
            "slug",
            "audio_file",
            "transcript",
            "transcript_synchronized",
            "cue_count",
            "order",
            "duration_seconds",
            "file_size_bytes",
            "read_along_offset_ms",
        ]
        read_only_fields = [
            "transcript_synchronized",
            "cue_count",
            "duration_seconds",
            "file_size_bytes",
        ]


class FlexibleDurationField(serializers.Field):
    """Reads back a float of seconds; accepts either a number of seconds or a
    "mm:ss" / "hh:mm:ss" string on write."""

    def to_representation(self, value):
        return value

    def to_internal_value(self, data):
        if data in (None, ""):
            return None
        seconds = parse_duration_seconds(data)
        if seconds is None or seconds < 0:
            raise serializers.ValidationError("Enter a duration in seconds or mm:ss.")
        return seconds


class VideoAdminSerializer(serializers.ModelSerializer):
    # Accepts a URL or bare id on write; the parsed 11-char id is returned.
    youtube_url = serializers.CharField()
    duration_seconds = FlexibleDurationField(required=False, allow_null=True)

    def _build_unique_slug(self, story, title: str, instance=None) -> str:
        base_slug = slugify(title) or "video"
        slug = base_slug
        suffix = 2
        queryset = Video.objects.filter(story=story)
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        while queryset.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    def validate_youtube_url(self, value):
        video_id = parse_youtube_id(value)
        if not video_id:
            raise serializers.ValidationError(
                "Enter a valid YouTube video URL or id."
            )
        self._parsed_youtube_id = video_id
        return value.strip()

    def validate(self, attrs):
        story = attrs.get("story") or getattr(self.instance, "story", None)
        title = attrs.get("title") or getattr(self.instance, "title", "")
        slug = attrs.get("slug")

        if story and title and (slug is None or str(slug).strip() == ""):
            attrs["slug"] = self._build_unique_slug(story, title, self.instance)

        parsed_id = getattr(self, "_parsed_youtube_id", None)
        if parsed_id:
            attrs["youtube_id"] = parsed_id
        return attrs

    class Meta:
        model = Video
        fields = ["id", "story", "title", "slug", "youtube_url", "youtube_id", "order", "duration_seconds"]
        read_only_fields = ["youtube_id"]


class SubmissionAdminSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    story_type = serializers.CharField(source="story_type.name", read_only=True)
    genres = GenreSerializer(many=True, read_only=True)
    cover_image = serializers.SerializerMethodField()
    cover_image_url = serializers.SerializerMethodField()
    pdf_file_url = serializers.SerializerMethodField()
    epub_file_url = serializers.SerializerMethodField()

    class Meta:
        model = Submission
        fields = [
            "id",
            "user",
            "user_email",
            "title",
            "about",
            "content",
            "story_type",
            "language",
            "genres",
            "cover_image",
            "cover_image_url",
            "notes",
            "pdf_file",
            "pdf_file_url",
            "epub_file",
            "epub_file_url",
            "status",
            "reviewer_notes",
            "published_story",
            "reviewed_by",
            "reviewed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "user",
            "user_email",
            "title",
            "about",
            "content",
            "story_type",
            "language",
            "genres",
            "cover_image",
            "cover_image_url",
            "notes",
            "pdf_file",
            "pdf_file_url",
            "epub_file",
            "epub_file_url",
            "published_story",
            "reviewed_by",
            "reviewed_at",
            "created_at",
            "updated_at",
        ]

    def get_cover_image(self, obj):
        if obj.cover_image_file:
            request = self.context.get("request")
            return request.build_absolute_uri(obj.cover_image_file.url) if request else obj.cover_image_file.url
        return obj.cover_image or ""

    def get_cover_image_url(self, obj):
        return self.get_cover_image(obj)

    def get_pdf_file_url(self, obj):
        if not obj.pdf_file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.pdf_file.url) if request else obj.pdf_file.url

    def get_epub_file_url(self, obj):
        if not obj.epub_file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.epub_file.url) if request else obj.epub_file.url


class StoryQueueSerializer(serializers.ModelSerializer):
    story_type = serializers.PrimaryKeyRelatedField(
        queryset=StoryType.objects.all(), required=False, allow_null=True
    )
    genres = serializers.PrimaryKeyRelatedField(queryset=Genre.objects.all(), many=True, required=False)
    categories = serializers.PrimaryKeyRelatedField(queryset=Category.objects.all(), many=True, required=False)
    tags = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all(), many=True, required=False)
    themes = serializers.PrimaryKeyRelatedField(queryset=Theme.objects.all(), many=True, required=False)
    published_date_label = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = StoryQueue
        fields = [
            "id",
            "title",
            "author_name",
            "about",
            "content",
            "notes",
            "story_type",
            "country",
            "language",
            "genres",
            "categories",
            "tags",
            "themes",
            "original_published_year",
            "original_published_month",
            "original_published_day",
            "published_date_label",
            "epub_link",
            "pdf_link",
            "cover_image_link",
            "is_added",
            "added_story",
            "created_at",
        ]
        read_only_fields = ["is_added", "added_story", "created_at"]

    def get_published_date_label(self, obj):
        return obj.published_date_display()

    def validate(self, attrs):
        # Same "only meaningful in combination" rule as StoryAdminSerializer
        # — a day without a month, or a month without a year, can't be
        # formatted into anything sensible.
        year = attrs.get("original_published_year", getattr(self.instance, "original_published_year", None))
        month = attrs.get("original_published_month", getattr(self.instance, "original_published_month", None))
        day = attrs.get("original_published_day", getattr(self.instance, "original_published_day", None))
        if month and not year:
            raise serializers.ValidationError({"original_published_month": "Requires the year to also be set."})
        if day and not month:
            raise serializers.ValidationError({"original_published_day": "Requires the month to also be set."})
        if year and month and day:
            try:
                date(year, month, day)
            except ValueError:
                raise serializers.ValidationError({"original_published_day": "Not a valid date."})
        return attrs
