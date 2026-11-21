from rest_framework import serializers

from .models import DigestDelivery, DigestIssue, NewsletterSubscription


class AdminSubscriberSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsletterSubscription
        fields = [
            "id",
            "email",
            "status",
            "source",
            "language",
            "send_weekday",
            "created_at",
            "confirmed_at",
            "last_digest_sent_at",
        ]
        read_only_fields = fields


class AdminDigestIssueSerializer(serializers.ModelSerializer):
    featured_story_title = serializers.SerializerMethodField()
    featured_story_slug = serializers.SerializerMethodField()

    def get_featured_story_title(self, obj: DigestIssue) -> str | None:
        return obj.featured_story.title if obj.featured_story_id else None

    def get_featured_story_slug(self, obj: DigestIssue) -> str | None:
        return obj.featured_story.slug if obj.featured_story_id else None

    class Meta:
        model = DigestIssue
        fields = [
            "id",
            "issue_date",
            "subject",
            "featured_story_title",
            "featured_story_slug",
            "status",
            "sent_count",
            "failed_count",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields


class AdminDigestDeliverySerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="subscription.email", read_only=True)

    class Meta:
        model = DigestDelivery
        fields = ["id", "email", "status", "error", "sent_at"]
        read_only_fields = fields


class AdminTestSendSerializer(serializers.Serializer):
    story_slug = serializers.SlugField(required=False, allow_blank=True, allow_null=True)
