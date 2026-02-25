from rest_framework import serializers
from .models import Audio, Story, Genre, Chapter, Tag, Author, Review, Submission


class GenreSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        return obj.stories.count()
    
    class Meta:
        model = Genre
        fields = ["id", "name", "stories_count"]


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name", "bio", "image", "stories_count"]


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

    def get_cover_image(self, obj):
        if obj.cover_image_file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.cover_image_file.url)
            return obj.cover_image_file.url
        return obj.cover_image or ""

    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "story_type",
            "published_date",
            "cover_image",
            "rating",
            "views",
            "has_audio",
            "genres",
            "reviews_count",
            "is_favorite",
            "favorites_count",
        ]


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
    genres = GenreSerializer(many=True, read_only=True)
    author = AuthorSerializer(read_only=True)
    chapter_count = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    is_favorite = serializers.SerializerMethodField()
    favorites_count = serializers.SerializerMethodField()
    chapters = ChapterListSerializer(many=True, read_only=True)
    audios = AudioSerializer(many=True, read_only=True)

    def get_chapter_count(self, obj):
        return obj.chapters.count()
    
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
        if obj.cover_image_file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.cover_image_file.url)
            return obj.cover_image_file.url
        return obj.cover_image or ""

    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "about",
            "genres",
            "story_type",
            "author",
            "published_date",
            "cover_image",
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
            "genres",
            "cover_image",
            "cover_image_file",
            "notes",
            "pdf_file",
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
            "genres",
            "cover_image",
            "status",
            "published_story",
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
