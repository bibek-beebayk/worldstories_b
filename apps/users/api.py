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
from smtplib import SMTPException
import logging
import socket
import time
import hmac
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
from rest_framework.views import APIView


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


def collect_smtp_diagnostics(extra_ports=None):
    host = getattr(settings, "EMAIL_HOST", "")
    configured_port = int(getattr(settings, "EMAIL_PORT", 0) or 0)
    email_timeout = int(getattr(settings, "EMAIL_TIMEOUT", 20) or 20)
    ports_to_check = [configured_port]
    for port in (extra_ports or []):
        if port and port not in ports_to_check:
            ports_to_check.append(port)

    result = {
        "email_backend": getattr(settings, "EMAIL_BACKEND", ""),
        "email_host": host,
        "email_port": configured_port,
        "email_use_tls": getattr(settings, "EMAIL_USE_TLS", ""),
        "email_use_ssl": getattr(settings, "EMAIL_USE_SSL", ""),
        "email_timeout": email_timeout,
        "default_from_email": getattr(settings, "DEFAULT_FROM_EMAIL", ""),
        "smtp_user_configured": bool(getattr(settings, "EMAIL_HOST_USER", "")),
        "smtp_password_configured": bool(getattr(settings, "EMAIL_HOST_PASSWORD", "")),
        "dns_lookup": "",
        "ports": {},
    }

    if not host:
        result["dns_lookup"] = "skipped (missing host)"
        return result

    try:
        resolved = socket.getaddrinfo(host, configured_port or 0, type=socket.SOCK_STREAM)
        result["dns_lookup"] = f"ok ({len(resolved)} records)"
    except OSError as exc:
        result["dns_lookup"] = f"failed: {exc}"
        for port in ports_to_check:
            result["ports"][str(port)] = "skipped (dns failed)"
        return result

    for port in ports_to_check:
        if not port:
            continue
        started = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=10):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                result["ports"][str(port)] = f"ok ({elapsed_ms}ms)"
        except OSError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result["ports"][str(port)] = f"failed after {elapsed_ms}ms: {exc}"
    return result


def can_use_email_test_endpoint(request):
    if settings.DEBUG:
        return True

    user = getattr(request, "user", None)
    if user and user.is_authenticated and user.is_staff:
        return True

    configured_key = str(getattr(settings, "EMAIL_TEST_API_KEY", "") or "").strip()
    provided_key = str(request.headers.get("X-Email-Test-Key", "") or "").strip()

    if configured_key and provided_key and hmac.compare_digest(configured_key, provided_key):
        return True
    return False


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
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()
        password = serializer.validated_data["password"]

        with transaction.atomic():
            user = User.objects.filter(email=email).first()

            if user and user.otp_verified:
                return Response(
                    {"message": "A user with this email already exists."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if not user:
                user = serializer.create({"email": email, "password": password})
            else:
                user.set_password(password)
                user.save(update_fields=["password"])

            OTP.objects.filter(
                user=user, otp_type="Registration", is_used=False
            ).update(is_used=True)
            otp = OTP.create(user.id, "Registration")

        try:
            self._send_registration_otp(user.email, otp.otp)
        except Exception:
            logger.exception(
                "OTP email send failed",
                extra={
                    "recipient": user.email,
                    "email_backend": getattr(settings, "EMAIL_BACKEND", ""),
                    "email_host": getattr(settings, "EMAIL_HOST", ""),
                    "email_port": getattr(settings, "EMAIL_PORT", ""),
                },
            )
            return Response(
                {"message": "Could not send OTP email. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "otp_required": True,
                "email": user.email,
                "message": "OTP sent to your email.",
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="validate-otp")
    def validate_otp(self, request):
        serializer = OTPValidateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].lower()
        otp_input = serializer.validated_data["otp"]

        user = User.objects.filter(email=email).first()
        if not user:
            return Response(
                {"message": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        otp_obj = OTP.objects.filter(
            user=user, otp_type="Registration", otp=otp_input, is_used=False
        ).first()
        if not otp_obj:
            return Response(
                {"message": "Invalid OTP."}, status=status.HTTP_400_BAD_REQUEST
            )

        if otp_obj.is_expired:
            otp_obj.is_used = True
            otp_obj.save(update_fields=["is_used"])
            return Response(
                {"message": "OTP expired. Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        otp_obj.is_used = True
        otp_obj.save(update_fields=["is_used"])
        user.otp_verified = True
        user.save(update_fields=["otp_verified"])

        tokens = get_tokens(user)
        return Response(
            {
                **tokens,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "username": user.username,
                },
            }
        )

    @action(detail=False, methods=["post"], url_path="resend-otp")
    def resend_otp(self, request):
        serializer = OTPResendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        user = User.objects.filter(email=email).first()
        if not user:
            return Response(
                {"message": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if user.otp_verified:
            return Response(
                {"message": "User is already verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        OTP.objects.filter(user=user, otp_type="Registration", is_used=False).update(
            is_used=True
        )
        otp = OTP.create(user.id, "Registration")

        try:
            self._send_registration_otp(user.email, otp.otp)
        except Exception:
            return Response(
                {"message": "Could not send OTP email. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"message": "OTP resent successfully."})

    @action(detail=False, methods=["post"])
    def login(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        if not user.otp_verified:
            return Response(
                {"message": "Please verify your email with OTP before logging in."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tokens = get_tokens(user)
        return Response(
            {
                **tokens,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "username": user.username,
                },
            }
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


class TestEmailAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not can_use_email_test_endpoint(request):
            return Response(
                {
                    "message": "Not allowed.",
                    "hint": "Use staff authentication or provide valid X-Email-Test-Key in production.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        recipient = (request.data.get("to") or "").strip()
        subject = (request.data.get("subject") or "").strip() or "WorldStories Test Email"
        message = (request.data.get("message") or "").strip() or "Test email from WorldStories."

        if not recipient:
            return Response(
                {"message": "'to' email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        diagnostics = collect_smtp_diagnostics()
        logger.info(
            "Test email requested",
            extra={"recipient": recipient, **diagnostics},
        )

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=getattr(
                    settings,
                    "DEFAULT_FROM_EMAIL",
                    getattr(settings, "EMAIL_HOST_USER", None),
                ),
                recipient_list=[recipient],
                fail_silently=False,
            )
            logger.info(
                "Test email sent successfully",
                extra={"recipient": recipient, **diagnostics},
            )
        except (OSError, SMTPException) as exc:
            logger.exception(
                "Test email send failed",
                extra={"recipient": recipient, **diagnostics},
            )
            return Response(
                {
                    "message": "SMTP connection failed.",
                    "detail": str(exc),
                    "email_host": getattr(settings, "EMAIL_HOST", ""),
                    "email_port": getattr(settings, "EMAIL_PORT", ""),
                    "diagnostics": diagnostics,
                    "hint": "Server cannot reach SMTP host/port. Check network egress/firewall and SMTP host/port.",
                },
                status=status.HTTP_502_BAD_GATEWAY,
        )
        return Response({"message": f"Email sent to {recipient}."}, status=status.HTTP_200_OK)


class TestEmailConfigAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        if not can_use_email_test_endpoint(request):
            return Response(
                {
                    "message": "Not allowed.",
                    "hint": "Use staff authentication or provide valid X-Email-Test-Key in production.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        diagnostics = collect_smtp_diagnostics(extra_ports=[587, 465])
        logger.info("SMTP self-check requested", extra=diagnostics)
        return Response(
            {
                "message": "SMTP diagnostics generated.",
                "diagnostics": diagnostics,
            },
            status=status.HTTP_200_OK,
        )
