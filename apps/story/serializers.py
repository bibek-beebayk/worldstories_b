from rest_framework import serializers
from django.utils.text import slugify
from core.libs.images import get_cover_image_url
from .models import Audio, Story, Genre, Chapter, Tag, Author, Review, Submission

CARD_COVER_SIZE = "480x640"
LARGE_COVER_SIZE = "900x1200"


class GenreSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        annotated_count = getattr(obj, "published_stories_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.stories.filter(is_published=True).count()
    
    class Meta:
        model = Genre
        fields = ["id", "name", "stories_count"]


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name", "bio", "image", "stories_count"]


class StoryUserSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    username = serializers.CharField()
    display_name = serializers.CharField(allow_null=True)


class AdminAuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name", "bio", "image"]


class AdminGenreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Genre
        fields = ["id", "name"]


class StoryListSerializer(serializers.ModelSerializer):
    genres = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    is_favorite = serializers.SerializerMethodField()
    favorites_count = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()

    def get_genres(self, obj):
        return list(obj.genres.values_list("name", flat=True)[:2])
    
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
            "reviews_count",
            "is_favorite",
            "favorites_count",
        ]


class FeaturedStorySerializer(StoryListSerializer):
    cover_image_size = LARGE_COVER_SIZE

    class Meta(StoryListSerializer.Meta):
        fields = StoryListSerializer.Meta.fields + ["about"]


class ChapterListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chapter
        fields = ["id", "title", "order", "slug"]


class ChapterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chapter
        fields = ["id", "title", "order", "content", "slug"]


class AudioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Audio
        fields = ["id", "title", "slug", "audio_file", "order"]


class AudioListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Audio
        fields = ["id", "title", "slug", "order"]


class StoryDetailSerializer(serializers.ModelSerializer):
    cover_image = serializers.SerializerMethodField()
    pdf_file = serializers.SerializerMethodField()
    epub_file = serializers.SerializerMethodField()
    genres = GenreSerializer(many=True, read_only=True)
    author = AuthorSerializer(read_only=True)
    submitted_by = serializers.SerializerMethodField()
    chapter_count = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    is_favorite = serializers.SerializerMethodField()
    favorites_count = serializers.SerializerMethodField()
    chapters = ChapterListSerializer(many=True, read_only=True)
    audios = AudioSerializer(many=True, read_only=True)
    translations = serializers.SerializerMethodField()

    def get_chapter_count(self, obj):
        return obj.chapters.count()

    def get_translations(self, obj):
        siblings = (
            Story.objects.filter(translation_group=obj.translation_group, is_published=True)
            .exclude(pk=obj.pk)
            .only("id", "slug", "language", "title")
            .order_by("language")
        )
        return [
            {"id": sibling.id, "slug": sibling.slug, "language": sibling.language, "title": sibling.title}
            for sibling in siblings
        ]
    
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
            "genres",
            "story_type",
            "language",
            "translations",
            "author",
            "submitted_by",
            "original_published_date",
            "cover_image",
            "pdf_file",
            "epub_file",
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
    author = serializers.PrimaryKeyRelatedField(
        queryset=Author.objects.all(), required=False, allow_null=True
    )
    genres = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), many=True, required=False
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

    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "about",
            "story_type",
            "language",
            "translations",
            "author",
            "submitted_by",
            "original_published_date",
            "site_published_date",
            "is_published",
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
            "is_completed",
            "genres",
            "tags",
            "rating",
            "views",
            "source",
        ]
        read_only_fields = ["rating", "views"]
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

        return super().update(instance, validated_data)

    def create(self, validated_data):
        # These flags are only meaningful for updates and are not model fields.
        validated_data.pop("remove_cover_image_file", None)
        validated_data.pop("remove_pdf_file", None)
        validated_data.pop("remove_epub_file", None)
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

    class Meta:
        model = Audio
        fields = ["id", "story", "title", "slug", "audio_file", "order"]


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
