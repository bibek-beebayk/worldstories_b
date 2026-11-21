from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import NewsletterSubscription, newsletter_token
from .serializers import (
    ConfirmSerializer,
    MeToggleSerializer,
    SubscribeSerializer,
    UnsubscribeByEmailSerializer,
    UnsubscribeSerializer,
)
from .services import assign_send_weekday, send_confirmation_email

SUBSCRIBE_RESPONSE = {
    "detail": "If this address can be subscribed, we sent a confirmation link."
}
UNSUBSCRIBE_RESPONSE = {
    "detail": "If that address was subscribed, it has been unsubscribed."
}


class NewsletterSubscribeAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "newsletter_subscribe"

    def post(self, request):
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        source = serializer.validated_data["source"]
        now = timezone.now()

        with transaction.atomic():
            subscription = (
                NewsletterSubscription.objects.select_for_update()
                .filter(email=email)
                .first()
            )
            if (
                subscription
                and subscription.status == NewsletterSubscription.STATUS_CONFIRMED
            ):
                return Response(SUBSCRIBE_RESPONSE, status=status.HTTP_202_ACCEPTED)
            if subscription is None:
                subscription = NewsletterSubscription(email=email, source=source)
            elif (
                subscription.confirmation_sent_at
                and subscription.confirmation_sent_at <= now - timedelta(hours=24)
            ):
                subscription.confirmation_send_count = 0

            subscription.status = NewsletterSubscription.STATUS_PENDING
            subscription.source = source
            subscription.confirm_token = newsletter_token()
            subscription.confirmed_token_hash = ""
            subscription.confirmed_at = None
            subscription.unsubscribed_at = None
            subscription.unsubscribe_reason = ""
            subscription.save()

            if subscription.confirmation_send_count >= 3:
                return Response(SUBSCRIBE_RESPONSE, status=status.HTTP_202_ACCEPTED)

            send_confirmation_email(subscription)
            subscription.confirmation_sent_at = now
            subscription.confirmation_send_count += 1
            subscription.save(
                update_fields=["confirmation_sent_at", "confirmation_send_count"]
            )

        return Response(SUBSCRIBE_RESPONSE, status=status.HTTP_202_ACCEPTED)


class NewsletterConfirmAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "newsletter_public"

    def post(self, request):
        serializer = ConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["token"]
        now = timezone.now()
        subscription = NewsletterSubscription.objects.filter(
            confirm_token=token
        ).first()
        if (
            subscription
            and subscription.confirmation_sent_at
            and subscription.confirmation_sent_at >= now - timedelta(days=7)
        ):
            subscription.status = NewsletterSubscription.STATUS_CONFIRMED
            subscription.confirmed_at = now
            subscription.confirmed_token_hash = NewsletterSubscription.token_hash(token)
            subscription.confirm_token = None
            subscription.unsubscribed_at = None
            subscription.unsubscribe_reason = ""
            if subscription.send_weekday is None:
                subscription.send_weekday = assign_send_weekday()
            subscription.save()
            return Response(
                {
                    "status": "confirmed",
                    "email": subscription.email,
                    "send_weekday": subscription.send_weekday,
                }
            )

        reused = NewsletterSubscription.objects.filter(
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirmed_token_hash=NewsletterSubscription.token_hash(token),
            confirmed_at__gte=now - timedelta(days=7),
        ).first()
        if reused:
            return Response(
                {
                    "status": "confirmed",
                    "email": reused.email,
                    "send_weekday": reused.send_weekday,
                }
            )
        return Response(
            {"code": "invalid_or_expired"}, status=status.HTTP_400_BAD_REQUEST
        )


class NewsletterUnsubscribeAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "newsletter_public"

    def post(self, request, token):
        subscription = NewsletterSubscription.objects.filter(
            unsubscribe_token=token
        ).first()
        if not subscription:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = UnsubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription.status = NewsletterSubscription.STATUS_UNSUBSCRIBED
        subscription.unsubscribed_at = subscription.unsubscribed_at or timezone.now()
        reason = serializer.validated_data.get("reason", "")
        if reason:
            subscription.unsubscribe_reason = reason
        subscription.save()
        return Response({"status": "unsubscribed"})


class NewsletterResubscribeAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "newsletter_public"

    def post(self, request, token):
        subscription = NewsletterSubscription.objects.filter(
            unsubscribe_token=token
        ).first()
        if not subscription:
            return Response(status=status.HTTP_404_NOT_FOUND)
        subscription.status = NewsletterSubscription.STATUS_CONFIRMED
        subscription.confirmed_at = timezone.now()
        subscription.confirm_token = None
        subscription.unsubscribed_at = None
        subscription.unsubscribe_reason = ""
        if subscription.send_weekday is None:
            subscription.send_weekday = assign_send_weekday()
        subscription.save()
        return Response(
            {
                "status": "confirmed",
                "email": subscription.email,
                "send_weekday": subscription.send_weekday,
            }
        )


class NewsletterUnsubscribeByEmailAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "newsletter_public"

    def post(self, request):
        serializer = UnsubscribeByEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription = NewsletterSubscription.objects.filter(
            email=serializer.validated_data["email"]
        ).first()
        if subscription:
            subscription.status = NewsletterSubscription.STATUS_UNSUBSCRIBED
            subscription.unsubscribed_at = (
                subscription.unsubscribed_at or timezone.now()
            )
            subscription.save()
        return Response(UNSUBSCRIBE_RESPONSE)


class NewsletterMeAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def _subscription(self, user):
        return (
            NewsletterSubscription.objects.filter(user=user).first()
            or NewsletterSubscription.objects.filter(
                email=user.email.strip().lower()
            ).first()
        )

    def get(self, request):
        subscription = self._subscription(request.user)
        return Response(
            {
                "status": subscription.status if subscription else "none",
                "email": request.user.email,
                "send_weekday": subscription.send_weekday if subscription else None,
            }
        )

    def post(self, request):
        serializer = MeToggleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = request.user.email.strip().lower()
        subscription, _ = NewsletterSubscription.objects.get_or_create(
            email=email,
            defaults={
                "user": request.user,
                "source": serializer.validated_data["source"],
            },
        )
        subscription.user = request.user
        if "source" in request.data or "subscribed" in serializer.validated_data:
            subscription.source = serializer.validated_data["source"]
        if "send_weekday" in serializer.validated_data:
            subscription.send_weekday = serializer.validated_data["send_weekday"]
        if "subscribed" in serializer.validated_data:
            if serializer.validated_data["subscribed"]:
                subscription.status = NewsletterSubscription.STATUS_CONFIRMED
                subscription.confirmed_at = timezone.now()
                subscription.confirm_token = None
                subscription.unsubscribed_at = None
                subscription.unsubscribe_reason = ""
                if subscription.send_weekday is None:
                    subscription.send_weekday = assign_send_weekday()
            else:
                subscription.status = NewsletterSubscription.STATUS_UNSUBSCRIBED
                subscription.unsubscribed_at = timezone.now()
        subscription.save()
        return Response(
            {
                "status": subscription.status,
                "email": subscription.email,
                "send_weekday": subscription.send_weekday,
            }
        )
