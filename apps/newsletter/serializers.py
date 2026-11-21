from rest_framework import serializers

from .models import NewsletterSubscription


class SubscribeSerializer(serializers.Serializer):
    email = serializers.EmailField()
    source = serializers.ChoiceField(
        choices=NewsletterSubscription.SOURCE_CHOICES, default="other"
    )

    def validate_email(self, value):
        return value.strip().lower()


class ConfirmSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=128)


class UnsubscribeSerializer(serializers.Serializer):
    reason = serializers.CharField(
        max_length=255, allow_blank=True, required=False, default=""
    )


class UnsubscribeByEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.strip().lower()


class MeToggleSerializer(serializers.Serializer):
    subscribed = serializers.BooleanField()
    source = serializers.ChoiceField(
        choices=NewsletterSubscription.SOURCE_CHOICES,
        required=False,
        default="profile",
    )
