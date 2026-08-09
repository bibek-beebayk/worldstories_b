import json

from rest_framework import serializers
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import QueryDict
from django.utils.text import slugify
from datetime import date
from core.libs.images import get_cover_image_url
from .models import (
    Audio,
    Story,
    Genre,
    Category,
    Chapter,
    Tag,
    Author,
    Review,
    Submission,
    published_story_q,
    with_preferred_translation_only,
)
from . import reading_time
from .audio_processing import normalize_uploaded_audio

CARD_COVER_SIZE = "480x640"
LARGE_COVER_SIZE = "900x1200"


class GenreSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()
    
    class Meta:
        model = Genre
        fields = ["id", "name", "stories_count"]


class CategorySerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(published_story_q()).count()

    class Meta:
        model = Category
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
    class Meta:
        model = Genre
        fields = ["id", "name"]


class AdminCategorySerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField(read_only=True)

    def get_stories_count(self, obj):
        return obj.stories.count()

    class Meta:
        model = Category
        fields = ["id", "name", "stories_count"]


class StoryListSerializer(serializers.ModelSerializer):
    genres = serializers.SerializerMethodField()
    categories = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    is_favorite = serializers.SerializerMethodField()
    favorites_count = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()

    def get_genres(self, obj):
        return list(obj.genres.values_list("name", flat=True)[:2])

    def get_categories(self, obj):
        return list(obj.categories.values_list("name", flat=True)[:2])

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
            "genres",
            "categories",
            "reviews_count",
            "is_favorite",
            "favorites_count",
        ]


class FeaturedStorySerializer(StoryListSerializer):
    cover_image_size = LARGE_COVER_SIZE

    class Meta(StoryListSerializer.Meta):
        fields = StoryListSerializer.Meta.fields + ["about"]


class AuthorDetailSerializer(AuthorSerializer):
    stories = serializers.SerializerMethodField()

    def get_stories(self, obj):
        stories = obj.stories.published().order_by("-site_published_date", "-id")
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


class AudioSerializer(serializers.ModelSerializer):
    download_size_bytes = serializers.IntegerField(source="file_size_bytes", read_only=True)

    class Meta:
        model = Audio
        fields = ["id", "title", "slug", "audio_file", "order", "download_size_bytes"]


class AudioListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Audio
        fields = ["id", "title", "slug", "order"]


class StoryDetailSerializer(serializers.ModelSerializer):
    cover_image = serializers.SerializerMethodField()
    pdf_file = serializers.SerializerMethodField()
    epub_file = serializers.SerializerMethodField()
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
    translations = serializers.SerializerMethodField()
    reading_time_minutes = serializers.SerializerMethodField()
    listening_time_minutes = serializers.SerializerMethodField()
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
        genre_ids = list(obj.genres.values_list("id", flat=True))
        matching = Q(story_type=obj.story_type) | Q(language=obj.language)
        if genre_ids:
            matching |= Q(genres__id__in=genre_ids)
        if obj.author_id:
            matching |= Q(author_id=obj.author_id)

        candidates = Story.objects.published().filter(matching).exclude(
            translation_group=obj.translation_group
        )
        candidates = with_preferred_translation_only(candidates).annotate(
            shared_genres=Count(
                "genres",
                filter=Q(genres__id__in=genre_ids),
                distinct=True,
            ),
            same_author=Case(
                *(
                    [When(author_id=obj.author_id, then=Value(1))]
                    if obj.author_id
                    else []
                ),
                default=Value(0),
                output_field=IntegerField(),
            ),
            same_story_type=Case(
                When(story_type=obj.story_type, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
            same_language=Case(
                When(language=obj.language, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        ).order_by(
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
            "tags",
            "rating",
            "views",
            "reviews_count",
            "is_favorite",
            "favorites_count",
            "chapter_count",
            "chapters",
            "audios",
            "reading_time_minutes",
            "listening_time_minutes",
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
            "story_type",
            "language",
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
            "genres",
            "categories",
            "tags",
            "rating",
            "views",
            "source",
        ]
        read_only_fields = ["rating", "views", "cached_file_reading_minutes"]
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


class AudioAdminSerializer(serializers.ModelSerializer):
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
        fields = ["id", "story", "title", "slug", "audio_file", "order", "duration_seconds", "file_size_bytes"]
        read_only_fields = ["duration_seconds", "file_size_bytes"]


class SubmissionAdminSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
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
