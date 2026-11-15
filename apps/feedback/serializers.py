from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import MAX_FEEDBACK_IMAGES, Feedback, FeedbackImage


class FeedbackCreateSerializer(serializers.ModelSerializer):
    message = serializers.CharField(min_length=3, max_length=5000)
    # Sent as repeated multipart `images` fields. Each file is checked against
    # FeedbackImage's own field validators (type, size) in validate_images.
    images = serializers.ListField(
        child=serializers.ImageField(), required=False, write_only=True, max_length=MAX_FEEDBACK_IMAGES
    )

    class Meta:
        model = Feedback
        fields = ["category", "rating", "message", "email", "page_url", "images"]

    def validate_page_url(self, value):
        return (value or "")[:500]

    def validate_images(self, files):
        image_field = FeedbackImage._meta.get_field("image")
        for file in files:
            try:
                image_field.run_validators(file)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.messages)
        return files

    def create(self, validated_data):
        files = validated_data.pop("images", [])
        feedback = super().create(validated_data)
        FeedbackImage.objects.bulk_create(FeedbackImage(feedback=feedback, image=file) for file in files)
        return feedback


class AdminFeedbackSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True, default=None)
    user_name = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()

    class Meta:
        model = Feedback
        fields = [
            "id",
            "user_email",
            "user_name",
            "email",
            "category",
            "rating",
            "message",
            "page_url",
            "user_agent",
            "images",
            "status",
            "admin_notes",
            "created_at",
            "updated_at",
        ]
        # Admins triage feedback; they don't edit what the sender wrote.
        read_only_fields = [field for field in fields if field not in {"status", "admin_notes"}]

    def get_user_name(self, obj):
        if not obj.user:
            return None
        return obj.user.display_name or obj.user.username

    def get_images(self, obj):
        request = self.context.get("request")
        return [
            {
                "id": item.id,
                "url": request.build_absolute_uri(item.image.url) if request else item.image.url,
            }
            for item in obj.images.all()
        ]
