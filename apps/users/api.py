from rest_framework import viewsets
from django.contrib.auth import get_user_model
from rest_framework.filters import SearchFilter
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework.response import Response
from django.conf import settings
from rest_framework import status
from django.core.mail import send_mail
from django.db import transaction
import logging
import os
from collections import defaultdict
from datetime import timedelta
from django.db.models import Count, Q, Sum
from django.utils import timezone
from .geo import record_login
from .models import OTP
from .recommendations import recommend_stories_for
from apps.story.api import IsSuperUser
from apps.story.models import Audio, Chapter, Favorite, Review, Story, Video
from apps.story.serializers import StoryListSerializer
from apps.stats.models import (
    AnalyticsEvent,
    StoryCompletion,
    ReadingProgress,
    ChapterReadingProgress,
    AudioReadingProgress,
    VideoWatchProgress,
    FileReadingProgress,
)
from apps.stats.streaks import compute_streak
from apps.story.excerpts import excerpt_at_progress
from .serializers import (
    ReadingHistoryItemSerializer,
    RegisterSerializer,
    LoginSerializer,
    OTPValidateSerializer,
    OTPResendSerializer,
    UserAdminSerializer,
    UserProfileSerializer,
    UserProfileUpdateSerializer,
    ContinueReadingItemSerializer,
    ContinueListeningItemSerializer,
    ContinueWatchingItemSerializer,
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



def card_stories_by_id(story_ids, user):
    """Story rows prepared for ``StoryListSerializer``, keyed by id.

    The library endpoints below walk progress rows and hand each row's
    ``progress.story`` straight to that serializer. Those stories came from a
    plain ``select_related``, so every card then re-queries its author,
    genres, categories, audios, videos, review count and favourite count —
    roughly seven queries per card, on an endpoint that returns a full page of
    them. Re-fetching the same stories once through ``for_card_list()``
    resolves all of that in a constant number of queries, and the favourite
    flags come back in one more instead of an ``.exists()`` per row.
    """
    story_ids = {story_id for story_id in story_ids if story_id}
    if not story_ids:
        return {}

    stories = {
        story.id: story
        for story in Story.objects.filter(id__in=story_ids).for_card_list()
    }

    if user and user.is_authenticated:
        favourited = set(
            Favorite.objects.filter(user=user, story_id__in=story_ids).values_list(
                "story_id", flat=True
            )
        )
        for story_id, story in stories.items():
            # The attribute StoryListSerializer.get_is_favorite reads before
            # falling back to a per-row query.
            story._is_favorite = story_id in favourited

    return stories


def related_counts_by_story(model, story_ids):
    """``{story_id: count}`` for a story's chapters / audios / videos.

    One grouped query for the whole page, replacing the ``story.chapters
    .count()`` that the overall-progress maths used to run inside the per-row
    loop. Stories with none are simply absent, so callers must treat a missing
    key as zero — which is also the "no items, so no meaningful overall
    progress" case they already guard for.
    """
    story_ids = {story_id for story_id in story_ids if story_id}
    if not story_ids:
        return {}
    return dict(
        model.objects.filter(story_id__in=story_ids)
        .values_list("story_id")
        .annotate(total=Count("id"))
    )


class AuthenticationViewSet(viewsets.GenericViewSet):
    queryset = User.objects.all()
    serializer_class = None  # Placeholder for actual serializer class
    permission_classes = [AllowAny]
    # Read by ScopedRateThrottle for whichever actions opt into it below —
    # both login and admin_login share this scope/rate (see
    # DEFAULT_THROTTLE_RATES["login"] in settings).
    throttle_scope = "login"

    def get_throttles(self):
        if self.action in {"login", "admin_login"}:
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_permissions(self):
        if self.action in {
            "me",
            "logout",
            "library_continue_reading",
            "library_completed_reading",
            "library_reading_history",
            "library_continue_listening",
            "library_continue_watching",
            "library_favorites",
            "library_reviews",
            "library_recommendations",
            "profile_insights",
            "reading_streak",
            "check_username",
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

    @action(detail=False, methods=["post"])
    def logout(self, request):
        # Blacklists the refresh token so it (and, with ROTATE_REFRESH_TOKENS,
        # any token minted from it) can't be used again — without this, the
        # access token expiring locally is the only thing that ends the
        # session; the refresh token would otherwise stay valid server-side
        # for its full lifetime even after the user has "logged out".
        # Missing/already-invalid tokens still return success — the caller's
        # goal (not being logged in anymore) is already satisfied either way.
        refresh_token = request.data.get("refresh")
        if refresh_token:
            try:
                RefreshToken(refresh_token).blacklist()
            except TokenError:
                pass
        return Response({"detail": "Logged out."}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="admin-login")
    def admin_login(self, request):
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""

        if not email or not password:
            return Response(
                {"message": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from django.contrib.auth import authenticate

        user = authenticate(request=request, username=email, password=password)
        if not user:
            return Response(
                {"message": "Invalid email or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_superuser:
            return Response(
                {"message": "Superuser access required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tokens = get_tokens(user)
        record_login(request, user)
        return Response(
            {
                "access": tokens["access"],
                "refresh": tokens["refresh"],
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "username": user.username,
                    "is_superuser": user.is_superuser,
                },
            },
            status=status.HTTP_200_OK,
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

        if not user.is_active:
            # JWTAuthentication already rejects tokens for a deactivated user
            # on every subsequent request (CHECK_USER_IS_ACTIVE), but this
            # flow mints tokens directly via RefreshToken.for_user() rather
            # than Django's authenticate() — so without this check, a
            # deactivated user could still complete Google login and walk
            # away with a fresh, working session.
            return Response(
                {"error": "This account has been deactivated."}, status=403
            )

        if not user.otp_verified:
            user.otp_verified = True
            user.save(update_fields=["otp_verified"])

        # Create JWT tokens
        refresh = RefreshToken.for_user(user)
        record_login(request, user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "name": name or user.username,
                    # Drives the "pick your genres" onboarding step on the
                    # frontend — `created` is a more direct first-login
                    # signal than login_count == 1 for this exact purpose,
                    # even though record_login() above now does increment it.
                    "is_first_login": created,
                },
            }
        )

    @action(detail=False, methods=["get"], url_path="username-available")
    def check_username(self, request):
        # Powers the debounced live-availability check in the onboarding/
        # profile-settings username field. Mirrors UserProfileUpdateSerializer.
        # validate_username's uniqueness check exactly (same exact-match
        # comparison, same self-exclusion) — that serializer is still the
        # actual source of truth on save, this just gives fast feedback
        # while typing.
        username = (request.query_params.get("username") or "").strip()
        if not username:
            return Response({"available": False, "detail": "Username is required."}, status=400)
        is_taken = User.objects.exclude(pk=request.user.pk).filter(username=username).exists()
        return Response({"available": not is_taken})

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

    @action(detail=False, methods=["get"], url_path="profile-insights")
    def profile_insights(self, request):
        """Aggregated, private reading-habit metrics for the profile overview."""

        reading_rows = list(
            ReadingProgress.objects.filter(user=request.user).values_list(
                "story_id", "progress", "updated_at"
            )
        )
        chapter_rows = list(
            ChapterReadingProgress.objects.filter(user=request.user).values_list(
                "story_id", "progress", "updated_at"
            )
        )
        file_rows = list(
            FileReadingProgress.objects.filter(user=request.user).values_list(
                "story_id", "format", "progress", "updated_at"
            )
        )
        audio_rows = list(
            AudioReadingProgress.objects.filter(user=request.user).values_list(
                "story_id", "progress", "updated_at"
            )
        )
        video_rows = list(
            VideoWatchProgress.objects.filter(user=request.user).values_list(
                "story_id", "progress", "updated_at"
            )
        )

        chapter_story_ids = {story_id for story_id, _, _ in chapter_rows}
        chapter_mode_ids = chapter_story_ids | {
            story_id for story_id, _, _ in reading_rows
        }
        epub_ids = {
            story_id for story_id, file_format, _, _ in file_rows if file_format == "epub"
        }
        pdf_ids = {
            story_id for story_id, file_format, _, _ in file_rows if file_format == "pdf"
        }
        audio_ids = {story_id for story_id, _, _ in audio_rows}
        video_ids = {story_id for story_id, _, _ in video_rows}
        started_ids = chapter_mode_ids | epub_ids | pdf_ids | audio_ids | video_ids

        # Read from the durable record rather than re-deriving completion from
        # progress counts, which is what the ~50 lines this replaces used to do
        # — three separate item-count queries and three defaultdict tallies,
        # all of which could disagree with the Completed list on the same page.
        # See apps/stats/completion.py.
        completed_ids = set(
            StoryCompletion.objects.filter(user=request.user).values_list(
                "story_id", flat=True
            )
        )

        def activity_date(value):
            if timezone.is_aware(value):
                return timezone.localtime(value).date()
            return value.date()

        today = timezone.localdate()
        activity = {
            today - timedelta(days=offset): {"reading": 0, "listening": 0, "watching": 0}
            for offset in range(13, -1, -1)
        }
        all_activity_dates = []

        # Chapter rows are the granular record for normal reading. Retain the
        # story-level timestamp only for legacy progress without chapter rows,
        # avoiding double-counting a single reader save.
        reading_timestamps = [updated_at for _, _, updated_at in chapter_rows]
        reading_timestamps.extend(
            updated_at
            for story_id, _, updated_at in reading_rows
            if story_id not in chapter_story_ids
        )
        reading_timestamps.extend(updated_at for _, _, _, updated_at in file_rows)
        listening_timestamps = [updated_at for _, _, updated_at in audio_rows]
        watching_timestamps = [updated_at for _, _, updated_at in video_rows]

        for updated_at in reading_timestamps:
            day = activity_date(updated_at)
            all_activity_dates.append(day)
            if day in activity:
                activity[day]["reading"] += 1
        for updated_at in listening_timestamps:
            day = activity_date(updated_at)
            all_activity_dates.append(day)
            if day in activity:
                activity[day]["listening"] += 1
        for updated_at in watching_timestamps:
            day = activity_date(updated_at)
            all_activity_dates.append(day)
            if day in activity:
                activity[day]["watching"] += 1

        genre_rows = list(
            Story.objects.filter(id__in=started_ids, genres__isnull=False)
            .values("genres__name")
            .annotate(value=Count("id", distinct=True))
            .order_by("-value", "genres__name")[:6]
        )
        genres = [
            {"name": row["genres__name"], "value": row["value"]}
            for row in genre_rows
        ]

        active_since = today - timedelta(days=29)
        active_days = len({day for day in all_activity_dates if day >= active_since})

        # 4.4: measured, not estimated. Session events carry how long the
        # reader actually had a reader or player open (see
        # useContentSessionAnalytics), which is a different and more honest
        # number than summing the stories' estimated durations — that would
        # credit a reader for the whole of a book they abandoned on page two.
        session_seconds = AnalyticsEvent.objects.filter(
            user=request.user,
            event_type__in=[
                AnalyticsEvent.EVENT_READING_SESSION,
                AnalyticsEvent.EVENT_LISTENING_SESSION,
                AnalyticsEvent.EVENT_WATCHING_SESSION,
            ],
        ).aggregate(total=Sum("duration_seconds"))["total"] or 0

        # Answerable today because Milestone 2.0 made completion durable; the
        # Story Passport (Milestone 5) builds the page around this same fact.
        countries_explored = (
            Story.objects.filter(id__in=completed_ids)
            .exclude(country="")
            .values("country")
            .distinct()
            .count()
        )

        return Response(
            {
                "summary": {
                    "titles_started": len(started_ids),
                    # Completions are counted from the record itself, not
                    # intersected with started_ids: a story finished on a
                    # device whose progress rows have since been pruned is
                    # still finished.
                    "titles_completed": len(completed_ids),
                    "active_days_30": active_days,
                    "favorite_genre": genres[0]["name"] if genres else None,
                    "total_reading_minutes": round(session_seconds / 60),
                    "countries_explored": countries_explored,
                },
                "activity": [
                    {
                        "date": day.isoformat(),
                        "reading": counts["reading"],
                        "listening": counts["listening"],
                        "watching": counts["watching"],
                    }
                    for day, counts in activity.items()
                ],
                "formats": [
                    {"name": "Chapters", "value": len(chapter_mode_ids)},
                    {"name": "EPUB", "value": len(epub_ids)},
                    {"name": "PDF", "value": len(pdf_ids)},
                    {"name": "Audio", "value": len(audio_ids)},
                    {"name": "Video", "value": len(video_ids)},
                ],
                "genres": genres,
            }
        )

    @action(detail=False, methods=["get"], url_path="reading-streak")
    def reading_streak(self, request):
        """Consecutive-day reading/listening streak. Derived from
        AnalyticsEvent (an append-only per-session log with a real
        created_at) rather than the ReadingProgress family (which only
        remembers the last day touched, not every day — see
        apps/stats/streaks.py)."""
        created_ats = AnalyticsEvent.objects.filter(
            user=request.user,
            event_type__in=[
                AnalyticsEvent.EVENT_READING_SESSION,
                AnalyticsEvent.EVENT_LISTENING_SESSION,
                AnalyticsEvent.EVENT_WATCHING_SESSION,
            ],
        ).values_list("created_at", flat=True)
        activity_dates = {timezone.localtime(created_at).date() for created_at in created_ats}
        current_streak, longest_streak = compute_streak(activity_dates, timezone.localdate())
        return Response({"current_streak": current_streak, "longest_streak": longest_streak})

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

        chapter_counts = related_counts_by_story(Chapter, story_ids)
        cards = card_stories_by_id(story_ids, request.user)

        payload = []
        for item in progress_qs:
            total_chapters = chapter_counts.get(item.story_id, 0)
            chapter_progress_map = chapter_progress_by_story.get(item.story_id, {})
            overall_progress = (
                sum(chapter_progress_map.values()) / total_chapters if total_chapters > 0 else 0.0
            )
            # Continue-reading should only contain in-progress stories.
            if overall_progress <= 0 or overall_progress >= 1:
                continue
            payload.append(
                {
                    "story": cards.get(item.story_id, item.story),
                    "chapter_slug": item.chapter.slug if item.chapter else None,
                    "chapter_title": item.chapter.title if item.chapter else None,
                    "chapter_progress": max(0.0, min(1.0, item.progress)),
                    "overall_progress": round(overall_progress, 4),
                    "updated_at": item.updated_at,
                    "excerpt": excerpt_at_progress(item.chapter, item.progress) if item.chapter else "",
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
        """Stories this reader has finished, newest first.

        Reads the durable StoryCompletion record rather than recomputing
        completion from chapter progress. The old derivation averaged
        ChapterReadingProgress over the story's chapter count, so an audiobook,
        a video, an EPUB or a PDF story could never appear here at all — none
        of them have chapters — and a story with no ReadingProgress row was
        invisible even when every chapter was finished. See
        apps/stats/completion.py.
        """
        completions = (
            StoryCompletion.objects.filter(user=request.user)
            .select_related("story")
            .order_by("-completed_at")
        )

        story_ids = [item.story_id for item in completions]
        cards = card_stories_by_id(story_ids, request.user)

        # The chapter the reader was last on, kept so a finished story still
        # links somewhere sensible. Absent for a story finished by listening,
        # watching or reading a file, which is expected rather than an error.
        last_chapter_by_story = {
            item.story_id: item.chapter
            for item in ReadingProgress.objects.filter(
                user=request.user, story_id__in=story_ids
            ).select_related("chapter")
            if item.chapter
        }

        payload = [
            {
                "story": cards.get(item.story_id, item.story),
                "chapter_slug": (
                    last_chapter_by_story[item.story_id].slug
                    if item.story_id in last_chapter_by_story
                    else None
                ),
                "chapter_title": (
                    last_chapter_by_story[item.story_id].title
                    if item.story_id in last_chapter_by_story
                    else None
                ),
                "chapter_progress": 1.0,
                "overall_progress": 1.0,
                "updated_at": item.completed_at,
                "excerpt": "",
            }
            for item in completions
        ]

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

        audio_counts = related_counts_by_story(Audio, story_ids)
        cards = card_stories_by_id(story_ids, request.user)

        payload = []
        for item in sorted(selected_items, key=lambda x: x.updated_at, reverse=True):
            story_audio_count = audio_counts.get(item.story_id, 0)
            story_progress_values = audio_progress_map.get(item.story_id, [])
            overall_progress = (
                sum(story_progress_values) / story_audio_count if story_audio_count > 0 else 0.0
            )
            payload.append(
                {
                    "story": cards.get(item.story_id, item.story),
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

    @action(detail=False, methods=["get"], url_path="library/continue-watching")
    def library_continue_watching(self, request):
        all_video_progress = (
            VideoWatchProgress.objects.filter(user=request.user)
            .select_related("story", "video")
            .order_by("story_id", "-updated_at")
        )

        latest_by_story = {}
        for item in all_video_progress:
            if item.story_id not in latest_by_story:
                latest_by_story[item.story_id] = item

        selected_items = [
            item for item in latest_by_story.values() if 0 < item.progress < 1
        ]
        story_ids = [item.story_id for item in selected_items]
        video_progress_qs = VideoWatchProgress.objects.filter(
            user=request.user, story_id__in=story_ids
        )
        video_progress_map = {}
        for item in video_progress_qs:
            video_progress_map.setdefault(item.story_id, []).append(
                max(0.0, min(1.0, item.progress))
            )

        video_counts = related_counts_by_story(Video, story_ids)
        cards = card_stories_by_id(story_ids, request.user)

        payload = []
        for item in sorted(selected_items, key=lambda x: x.updated_at, reverse=True):
            story_video_count = video_counts.get(item.story_id, 0)
            story_progress_values = video_progress_map.get(item.story_id, [])
            overall_progress = (
                sum(story_progress_values) / story_video_count if story_video_count > 0 else 0.0
            )
            payload.append(
                {
                    "story": cards.get(item.story_id, item.story),
                    "video_slug": item.video.slug if item.video else None,
                    "video_title": item.video.title if item.video else None,
                    "video_progress": max(0.0, min(1.0, item.progress)),
                    "overall_progress": round(overall_progress, 4),
                    "updated_at": item.updated_at,
                }
            )

        page = self.paginate_queryset(payload)
        if page is not None:
            serializer = ContinueWatchingItemSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)
        serializer = ContinueWatchingItemSerializer(
            payload, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="library/reading-history")
    def library_reading_history(self, request):
        """Everything this reader has opened, most recently touched first.

        Distinct from Continue Reading, which is only what is *unfinished*, and
        from Completed, which is only what is *finished*. History is the whole
        record — the answer to "what was that story I read a few weeks ago".

        Derived from the progress rows that already exist rather than a new
        table: every surface stamps `updated_at` on save, so the latest across
        a story's rows is when the reader last touched it. `progress` is the
        furthest of those rows, so a story read to the end as an audiobook
        reads as complete even if its text was barely opened.
        """
        sources = [
            (ReadingProgress, "reading"),
            (ChapterReadingProgress, "chapter"),
            (FileReadingProgress, "file"),
            (AudioReadingProgress, "audio"),
            (VideoWatchProgress, "video"),
        ]

        last_read_at = {}
        furthest_progress = {}
        for model, _label in sources:
            rows = model.objects.filter(user=request.user).values_list(
                "story_id", "progress", "updated_at"
            )
            for story_id, progress, updated_at in rows:
                if story_id is None:
                    continue
                progress = max(0.0, min(1.0, progress or 0.0))
                seen_at = last_read_at.get(story_id)
                # Membership check rather than a sentinel default: datetime.min
                # is naive and these are timezone-aware, which raises rather
                # than comparing.
                if seen_at is None or updated_at > seen_at:
                    last_read_at[story_id] = updated_at
                furthest_progress[story_id] = max(
                    furthest_progress.get(story_id, 0.0), progress
                )

        story_ids = sorted(last_read_at, key=lambda sid: last_read_at[sid], reverse=True)
        cards = card_stories_by_id(story_ids, request.user)
        completed_ids = set(
            StoryCompletion.objects.filter(
                user=request.user, story_id__in=story_ids
            ).values_list("story_id", flat=True)
        )

        payload = [
            {
                "story": cards[story_id],
                "last_read_at": last_read_at[story_id],
                "progress": round(furthest_progress.get(story_id, 0.0), 4),
                # From the record, not from the progress fraction: a story can
                # be complete while no single surface reads 100%.
                "completed": story_id in completed_ids,
            }
            for story_id in story_ids
            # A story unpublished since it was read has no card to show.
            if story_id in cards
        ]

        page = self.paginate_queryset(payload)
        if page is not None:
            serializer = ReadingHistoryItemSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)
        serializer = ReadingHistoryItemSerializer(
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

    @action(detail=False, methods=["get"], url_path="library/recommendations")
    def library_recommendations(self, request):
        require_summary = request.query_params.get("quick_read") == "true"
        exclude_slug = request.query_params.get("exclude")
        exclude_story_id = (
            Story.objects.filter(slug=exclude_slug).values_list("id", flat=True).first()
            if exclude_slug
            else None
        )
        stories = recommend_stories_for(
            request.user, require_summary=require_summary, exclude_story_id=exclude_story_id
        )
        serializer = StoryListSerializer(stories, many=True, context={"request": request})
        return Response(serializer.data)


class UserAdminViewSet(viewsets.ModelViewSet):
    """List/search/view users and manage their staff, superuser, and active
    status from the admin panel. No create action — accounts are only ever
    provisioned via Google login, never manually. No delete action either:
    hard-deleting a user cascades away their reviews, favorites, and
    submissions, so deactivation (is_active) is the reversible alternative
    exposed here instead."""

    http_method_names = ["get", "patch", "head", "options"]
    queryset = User.objects.all().order_by("-date_joined")
    serializer_class = UserAdminSerializer
    permission_classes = [IsSuperUser]
    filter_backends = [SearchFilter]
    search_fields = ["email", "username", "display_name"]

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        # Checked against validated_data, not raw request.data — a
        # multipart/form request would otherwise arrive as the string
        # "False" rather than the boolean False, silently slipping past an
        # `is False` check on the unparsed value.
        if instance.pk == request.user.pk:
            attempting_self_lockout = (
                serializer.validated_data.get("is_active") is False
                or serializer.validated_data.get("is_superuser") is False
            )
            if attempting_self_lockout:
                return Response(
                    {"detail": "You can't deactivate or demote your own account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        self.perform_update(serializer)
        return Response(serializer.data)
