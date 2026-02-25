from rest_framework import viewsets
from django.contrib.auth import get_user_model
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.response import Response
from django.conf import settings
from rest_framework import status
from django.core.mail import send_mail
from django.db import transaction
import logging
import os
from .models import OTP
from apps.story.models import Favorite, Review
from apps.story.serializers import StoryListSerializer
from apps.stats.models import ReadingProgress, ChapterReadingProgress, AudioReadingProgress
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    OTPValidateSerializer,
    OTPResendSerializer,
    UserProfileSerializer,
    UserProfileUpdateSerializer,
    ContinueReadingItemSerializer,
    ContinueListeningItemSerializer,
    FavoriteItemSerializer,
    MyReviewItemSerializer,
)


User = get_user_model()
logger = logging.getLogger(__name__)


def get_tokens(user):
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def build_unique_username(base_value: str) -> str:
    base_username = (base_value or "user").strip().lower()
    username = base_username
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f"{base_username}{suffix}"
        suffix += 1
    return username


class AuthenticationViewSet(viewsets.GenericViewSet):
    queryset = User.objects.all()
    serializer_class = None  # Placeholder for actual serializer class
    permission_classes = [AllowAny]

    def get_permissions(self):
        if self.action in {
            "me",
            "library_continue_reading",
            "library_completed_reading",
            "library_continue_listening",
            "library_favorites",
            "library_reviews",
        }:
            from rest_framework.permissions import IsAuthenticated

            return [IsAuthenticated()]
        return super().get_permissions()

    def _send_registration_otp(self, email: str, code: int):
        logger.info(
            "Sending registration OTP email",
            extra={
                "recipient": email,
                "email_backend": getattr(settings, "EMAIL_BACKEND", ""),
                "email_host": getattr(settings, "EMAIL_HOST", ""),
                "email_port": getattr(settings, "EMAIL_PORT", ""),
                "email_use_tls": getattr(settings, "EMAIL_USE_TLS", ""),
                "email_use_ssl": getattr(settings, "EMAIL_USE_SSL", ""),
                "default_from_email": getattr(settings, "DEFAULT_FROM_EMAIL", ""),
            },
        )
        send_mail(
            subject="WorldStories OTP Verification",
            message=f"Your OTP code is {code}. It expires in 10 minutes.",
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                getattr(settings, "EMAIL_HOST_USER", None),
            ),
            recipient_list=[email],
            fail_silently=False,
        )


    @action(detail=False, methods=["post"])
    def register(self, request):
        return Response(
            {"message": "Email/password registration is disabled. Use Google login."},
            status=status.HTTP_410_GONE,
        )

    @action(detail=False, methods=["post"], url_path="validate-otp")
    def validate_otp(self, request):
        return Response(
            {"message": "OTP verification is disabled. Use Google login."},
            status=status.HTTP_410_GONE,
        )

    @action(detail=False, methods=["post"], url_path="resend-otp")
    def resend_otp(self, request):
        return Response(
            {"message": "OTP resend is disabled. Use Google login."},
            status=status.HTTP_410_GONE,
        )

    @action(detail=False, methods=["post"])
    def login(self, request):
        return Response(
            {"message": "Email/password login is disabled. Use Google login."},
            status=status.HTTP_410_GONE,
        )

    @action(detail=False, methods=["post"], url_path="google-login")
    def google_login(self, request):
        token = request.data.get("token")
        google_client_id = getattr(settings, "GOOGLE_CLIENT_ID", "") or os.environ.get(
            "GOOGLE_CLIENT_ID", ""
        )

        if not token:
            return Response({"error": "Token missing"}, status=400)
        if not google_client_id:
            return Response({"error": "Google OAuth is not configured."}, status=500)

        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests

            # Verify token
            idinfo = id_token.verify_oauth2_token(
                token, google_requests.Request(), google_client_id
            )

            email = idinfo["email"]
            name = idinfo.get("name", "")

        except Exception:
            return Response({"error": "Invalid Google token"}, status=400)

        # Create user if not exists
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "username": build_unique_username(email.split("@")[0]),
                "otp_verified": True,
            },
        )

        if not user.otp_verified:
            user.otp_verified = True
            user.save(update_fields=["otp_verified"])

        # Create JWT tokens
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "name": name or user.username,
                },
            }
        )

    @action(detail=False, methods=["get", "patch"], url_path="me")
    def me(self, request):
        if request.method == "GET":
            serializer = UserProfileSerializer(request.user)
            return Response(serializer.data)

        serializer = UserProfileUpdateSerializer(
            request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserProfileSerializer(request.user).data)

    @action(detail=False, methods=["get"], url_path="library/continue-reading")
    def library_continue_reading(self, request):
        progress_qs = (
            ReadingProgress.objects.filter(user=request.user)
            .select_related("story", "chapter")
            .order_by("-updated_at")
        )
        story_ids = [item.story_id for item in progress_qs if item.story_id]
        chapter_progress_qs = ChapterReadingProgress.objects.filter(
            user=request.user, story_id__in=story_ids
        ).select_related("chapter")

        chapter_progress_by_story = {}
        for item in chapter_progress_qs:
            chapter_progress_by_story.setdefault(item.story_id, {})[
                item.chapter.slug
            ] = max(0.0, min(1.0, item.progress))

        payload = []
        for item in progress_qs:
            total_chapters = item.story.chapters.count()
            chapter_progress_map = chapter_progress_by_story.get(item.story_id, {})
            overall_progress = (
                sum(chapter_progress_map.values()) / total_chapters if total_chapters > 0 else 0.0
            )
            # Continue-reading should only contain in-progress stories.
            if overall_progress <= 0 or overall_progress >= 1:
                continue
            payload.append(
                {
                    "story": item.story,
                    "chapter_slug": item.chapter.slug if item.chapter else None,
                    "chapter_title": item.chapter.title if item.chapter else None,
                    "chapter_progress": max(0.0, min(1.0, item.progress)),
                    "overall_progress": round(overall_progress, 4),
                    "updated_at": item.updated_at,
                }
            )

        page = self.paginate_queryset(payload)
        if page is not None:
            serializer = ContinueReadingItemSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)
        serializer = ContinueReadingItemSerializer(
            payload, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="library/completed-reading")
    def library_completed_reading(self, request):
        progress_qs = (
            ReadingProgress.objects.filter(user=request.user)
            .select_related("story", "chapter")
            .order_by("-updated_at")
        )
        story_ids = [item.story_id for item in progress_qs if item.story_id]
        chapter_progress_qs = ChapterReadingProgress.objects.filter(
            user=request.user, story_id__in=story_ids
        ).select_related("chapter")

        chapter_progress_by_story = {}
        for item in chapter_progress_qs:
            chapter_progress_by_story.setdefault(item.story_id, {})[
                item.chapter.slug
            ] = max(0.0, min(1.0, item.progress))

        payload = []
        for item in progress_qs:
            total_chapters = item.story.chapters.count()
            chapter_progress_map = chapter_progress_by_story.get(item.story_id, {})
            overall_progress = (
                sum(chapter_progress_map.values()) / total_chapters if total_chapters > 0 else 0.0
            )
            if overall_progress < 1:
                continue
            payload.append(
                {
                    "story": item.story,
                    "chapter_slug": item.chapter.slug if item.chapter else None,
                    "chapter_title": item.chapter.title if item.chapter else None,
                    "chapter_progress": max(0.0, min(1.0, item.progress)),
                    "overall_progress": 1.0,
                    "updated_at": item.updated_at,
                }
            )

        page = self.paginate_queryset(payload)
        if page is not None:
            serializer = ContinueReadingItemSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)
        serializer = ContinueReadingItemSerializer(
            payload, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="library/continue-listening")
    def library_continue_listening(self, request):
        all_audio_progress = (
            AudioReadingProgress.objects.filter(user=request.user)
            .select_related("story", "audio")
            .order_by("story_id", "-updated_at")
        )

        latest_by_story = {}
        for item in all_audio_progress:
            if item.story_id not in latest_by_story:
                latest_by_story[item.story_id] = item

        selected_items = [
            item for item in latest_by_story.values() if 0 < item.progress < 1
        ]
        story_ids = [item.story_id for item in selected_items]
        audio_progress_qs = AudioReadingProgress.objects.filter(
            user=request.user, story_id__in=story_ids
        )
        audio_progress_map = {}
        for item in audio_progress_qs:
            audio_progress_map.setdefault(item.story_id, []).append(
                max(0.0, min(1.0, item.progress))
            )

        payload = []
        for item in sorted(selected_items, key=lambda x: x.updated_at, reverse=True):
            story_audio_count = item.story.audios.count()
            story_progress_values = audio_progress_map.get(item.story_id, [])
            overall_progress = (
                sum(story_progress_values) / story_audio_count if story_audio_count > 0 else 0.0
            )
            payload.append(
                {
                    "story": item.story,
                    "audio_slug": item.audio.slug if item.audio else None,
                    "audio_title": item.audio.title if item.audio else None,
                    "audio_progress": max(0.0, min(1.0, item.progress)),
                    "overall_progress": round(overall_progress, 4),
                    "updated_at": item.updated_at,
                }
            )

        page = self.paginate_queryset(payload)
        if page is not None:
            serializer = ContinueListeningItemSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)
        serializer = ContinueListeningItemSerializer(
            payload, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="library/favorites")
    def library_favorites(self, request):
        queryset = (
            Favorite.objects.filter(user=request.user)
            .select_related("story")
            .order_by("-created_at")
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = FavoriteItemSerializer(page, many=True, context={"request": request})
            return self.get_paginated_response(serializer.data)
        serializer = FavoriteItemSerializer(queryset, many=True, context={"request": request})
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="library/reviews")
    def library_reviews(self, request):
        queryset = (
            Review.objects.filter(user=request.user)
            .select_related("story")
            .order_by("-updated_at")
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = MyReviewItemSerializer(page, many=True, context={"request": request})
            return self.get_paginated_response(serializer.data)
        serializer = MyReviewItemSerializer(queryset, many=True, context={"request": request})
        return Response(serializer.data)
