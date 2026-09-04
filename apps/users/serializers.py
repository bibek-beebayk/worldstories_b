from math import ceil

from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.story import reading_time
from apps.story.models import Favorite, Genre, Review, Submission
from apps.story.serializers import GenreSerializer, StoryListSerializer


User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)

    def _build_unique_username(self, email):
        base_username = email.split("@")[0].strip().lower() or "user"
        username = base_username
        suffix = 1

        while User.objects.filter(username=username).exists():
            username = f"{base_username}{suffix}"
            suffix += 1

        return username

    def create(self, validated_data):
        email = validated_data["email"]
        password = validated_data["password"]
        username = self._build_unique_username(email)
        return User.objects.create_user(email=email, password=password, username=username)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        user = authenticate(
            request=self.context.get("request"),
            username=email,
            password=password,
        )

        if not user:
            raise serializers.ValidationError("Invalid email or password.")

        attrs["user"] = user
        return attrs


class OTPValidateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.IntegerField()


class OTPResendSerializer(serializers.Serializer):
    email = serializers.EmailField()


class UserProfileSerializer(serializers.ModelSerializer):
    favorites_count = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    reading_in_progress_count = serializers.SerializerMethodField()
    listening_in_progress_count = serializers.SerializerMethodField()
    watching_in_progress_count = serializers.SerializerMethodField()
    preferred_genres = GenreSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "is_superuser",
            "display_name",
            "bio",
            "avatar_url",
            "date_joined",
            "favorites_count",
            "reviews_count",
            "reading_in_progress_count",
            "listening_in_progress_count",
            "watching_in_progress_count",
            "preferred_genres",
        ]

    def get_favorites_count(self, obj):
        return Favorite.objects.filter(user=obj).count()

    def get_reviews_count(self, obj):
        return Review.objects.filter(user=obj).count()

    def get_reading_in_progress_count(self, obj):
        return obj.reading_progress.filter(progress__gt=0, progress__lt=1).count()

    def get_listening_in_progress_count(self, obj):
        return obj.audio_reading_progress.filter(progress__gt=0, progress__lt=1).count()

    def get_watching_in_progress_count(self, obj):
        return obj.video_watch_progress.filter(progress__gt=0, progress__lt=1).count()


class UserAdminSerializer(serializers.ModelSerializer):
    """Powers the admin-panel Users page. Only is_staff/is_superuser/is_active
    are writable here — everything else (identity, join date, activity
    counts) is admin-visible but not editable from this endpoint."""

    favorites_count = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    submissions_count = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "display_name",
            "avatar_url",
            "date_joined",
            "last_login",
            "login_count",
            "otp_verified",
            "is_staff",
            "is_superuser",
            "is_active",
            "favorites_count",
            "reviews_count",
            "submissions_count",
        ]
        read_only_fields = [
            "id",
            "email",
            "username",
            "display_name",
            "avatar_url",
            "date_joined",
            "last_login",
            "login_count",
            "otp_verified",
            "favorites_count",
            "reviews_count",
            "submissions_count",
        ]

    def get_favorites_count(self, obj):
        return Favorite.objects.filter(user=obj).count()

    def get_reviews_count(self, obj):
        return Review.objects.filter(user=obj).count()

    def get_submissions_count(self, obj):
        return Submission.objects.filter(user=obj).count()


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    preferred_genres = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), many=True, required=False
    )

    class Meta:
        model = User
        fields = ["username", "display_name", "bio", "avatar_url", "preferred_genres"]

    def validate_username(self, value):
        username = value.strip()
        instance = self.instance
        if User.objects.exclude(id=getattr(instance, "id", None)).filter(username=username).exists():
            raise serializers.ValidationError("This username is already taken.")
        return username


class RemainingMinutesMixin(serializers.Serializer):
    """`~8 min remaining` for a partly-finished item.

    Derived from the story's existing time estimate rather than a second
    calculation, and served from the API so every surface that shows it agrees.
    None when the story has no estimate at all — no chapters and no cached
    file-derived value — so the UI can omit the line rather than print
    "~0 min remaining" for a story whose length is simply unknown.

    These payloads are plain dicts built in the library views, not model
    instances, hence the subscript access.
    """

    remaining_minutes = serializers.SerializerMethodField()

    @staticmethod
    def total_minutes(story):
        """The whole-item estimate this surface counts down from. Overridden
        by the listening/watching variants, whose totals come from media
        duration rather than words on a page."""
        return reading_time.story_reading_minutes_cached(story)

    def get_remaining_minutes(self, obj):
        total = self.total_minutes(obj["story"])
        if not total:
            return None
        progress = max(0.0, min(1.0, obj["overall_progress"] or 0.0))
        remaining = total * (1.0 - progress)
        if remaining <= 0:
            return 0
        # Rounded up, so the last stretch of a story reads as "~1 min
        # remaining" rather than "~0 min remaining".
        return max(1, ceil(remaining))


class ContinueReadingItemSerializer(RemainingMinutesMixin):
    story = StoryListSerializer()
    chapter_slug = serializers.CharField(allow_null=True)
    chapter_title = serializers.CharField(allow_null=True)
    chapter_progress = serializers.FloatField()
    overall_progress = serializers.FloatField()
    updated_at = serializers.DateTimeField()
    excerpt = serializers.CharField(allow_blank=True)


class ReadingHistoryItemSerializer(serializers.Serializer):
    """One row of "everything you have opened".

    Deliberately not a RemainingMinutesMixin subclass: history is a record of
    what happened, not an invitation to continue, and a "~8 min remaining" on a
    story finished last year would be noise.
    """

    story = StoryListSerializer()
    last_read_at = serializers.DateTimeField()
    progress = serializers.FloatField()
    completed = serializers.BooleanField()


class ContinueListeningItemSerializer(RemainingMinutesMixin):
    @staticmethod
    def total_minutes(story):
        return reading_time.story_listening_minutes(story.audios.all())

    story = StoryListSerializer()
    audio_slug = serializers.CharField(allow_null=True)
    audio_title = serializers.CharField(allow_null=True)
    audio_progress = serializers.FloatField()
    overall_progress = serializers.FloatField()
    updated_at = serializers.DateTimeField()


class ContinueWatchingItemSerializer(RemainingMinutesMixin):
    @staticmethod
    def total_minutes(story):
        return reading_time.story_watch_minutes(story.videos.all())

    story = StoryListSerializer()
    video_slug = serializers.CharField(allow_null=True)
    video_title = serializers.CharField(allow_null=True)
    video_progress = serializers.FloatField()
    overall_progress = serializers.FloatField()
    updated_at = serializers.DateTimeField()


class FavoriteItemSerializer(serializers.ModelSerializer):
    story = StoryListSerializer(read_only=True)

    class Meta:
        model = Favorite
        fields = ["id", "story", "created_at"]


class MyReviewItemSerializer(serializers.ModelSerializer):
    story = StoryListSerializer(read_only=True)

    class Meta:
        model = Review
        fields = ["id", "story", "rating", "comment", "created_at", "updated_at"]
