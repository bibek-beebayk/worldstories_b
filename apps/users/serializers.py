from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.story.models import Favorite, Review
from apps.story.serializers import StoryListSerializer


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

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "display_name",
            "bio",
            "avatar_url",
            "date_joined",
            "favorites_count",
            "reviews_count",
            "reading_in_progress_count",
            "listening_in_progress_count",
        ]

    def get_favorites_count(self, obj):
        return Favorite.objects.filter(user=obj).count()

    def get_reviews_count(self, obj):
        return Review.objects.filter(user=obj).count()

    def get_reading_in_progress_count(self, obj):
        return obj.reading_progress.filter(progress__gt=0, progress__lt=1).count()

    def get_listening_in_progress_count(self, obj):
        return obj.audio_reading_progress.filter(progress__gt=0, progress__lt=1).count()


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["username", "display_name", "bio", "avatar_url"]

    def validate_username(self, value):
        username = value.strip()
        instance = self.instance
        if User.objects.exclude(id=getattr(instance, "id", None)).filter(username=username).exists():
            raise serializers.ValidationError("This username is already taken.")
        return username


class ContinueReadingItemSerializer(serializers.Serializer):
    story = StoryListSerializer()
    chapter_slug = serializers.CharField(allow_null=True)
    chapter_title = serializers.CharField(allow_null=True)
    chapter_progress = serializers.FloatField()
    overall_progress = serializers.FloatField()
    updated_at = serializers.DateTimeField()


class ContinueListeningItemSerializer(serializers.Serializer):
    story = StoryListSerializer()
    audio_slug = serializers.CharField(allow_null=True)
    audio_title = serializers.CharField(allow_null=True)
    audio_progress = serializers.FloatField()
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
