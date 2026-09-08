"""Isolated ingestion and superuser reporting for nepalikatha.worldstories.net."""
from django.db.models import Count, Sum, Q, CharField, Value
from django.db.models.functions import Cast, Concat
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import SimpleRateThrottle

from apps.story.api import IsSuperUser, is_untracked_request
from apps.story.models import Story, published_story_q
from apps.story.analytics_api import get_range_days, get_cutoff, get_time_interval, time_trunc, fill_time_buckets
from .models import NepalikathaEvent


class NepalikathaEventSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    event_type = serializers.ChoiceField(choices=["visit", "read"])
    visitor_id = serializers.UUIDField()
    session_id = serializers.UUIDField()
    path = serializers.CharField(max_length=500)
    story_slug = serializers.SlugField(max_length=255, allow_unicode=True, required=False, default="")
    chapter_slug = serializers.SlugField(max_length=255, allow_unicode=True, required=False, default="")
    duration_seconds = serializers.IntegerField(min_value=0, max_value=30, default=0)

    def validate(self, data):
        if not data["path"].startswith("/") or data["path"].startswith("//") or "?" in data["path"]:
            raise serializers.ValidationError("path must be a local pathname without search parameters.")
        if data["event_type"] == "read":
            story = Story.objects.filter(published_story_q(), show_in_nepali_site=True, slug=data["story_slug"]).first()
            if not story or not story.chapters.filter(slug=data["chapter_slug"]).exists():
                raise serializers.ValidationError("A published Nepalikatha story and its chapter are required.")
            if not data["duration_seconds"]:
                raise serializers.ValidationError("Reading events must contain active time.")
            data["story"] = story
        elif data["duration_seconds"] or data["story_slug"] or data["chapter_slug"]:
            raise serializers.ValidationError("Visit events cannot contain reading data.")
        data.pop("chapter_slug")
        return data


class NepalikathaThrottle(SimpleRateThrottle):
    scope = "nepalikatha-events"

    def get_rate(self):
        return "120/min"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class NepalikathaEventAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [NepalikathaThrottle]

    def post(self, request):
        if is_untracked_request(request, companion=True):
            return Response(status=status.HTTP_202_ACCEPTED)
        serializer = NepalikathaEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        event_id = values.pop("event_id")
        # UUID retries are idempotent, including concurrent requests; no shared counters.
        NepalikathaEvent.objects.get_or_create(event_id=event_id, defaults=values)
        return Response(status=status.HTTP_202_ACCEPTED)


class AdminNepalikathaAnalyticsAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        days = get_range_days(request)
        events = NepalikathaEvent.objects.filter(created_at__gte=get_cutoff(days), created_at__lte=timezone.now())
        reads = events.filter(event_type="read")
        seconds = reads.aggregate(total=Sum("duration_seconds"))["total"] or 0
        readers = reads.values("visitor_id").distinct().count()
        sessions = reads.values("visitor_id", "session_id", "story_slug").distinct().count()
        series = events.annotate(day=time_trunc("created_at", days)).values("day").annotate(
            visitors=Count("visitor_id", distinct=True),
            page_views=Count("id", filter=Q(event_type="visit")),
            reading_seconds=Sum("duration_seconds"),
        ).order_by("day")
        top = reads.annotate(reader_session=Concat(
            Cast("visitor_id", CharField()), Value(":"), Cast("session_id", CharField()),
        )).values("story_slug", "story__title").annotate(
            readers=Count("visitor_id", distinct=True),
            reads=Count("reader_session", distinct=True),
            reading_seconds=Sum("duration_seconds"),
        ).order_by("-reading_seconds", "story_slug")[:20]
        return Response({
            "range_days": days,
            "time_interval": get_time_interval(days),
            "visitors": events.values("visitor_id").distinct().count(),
            "page_views": events.filter(event_type="visit").count(),
            "readers": readers,
            "stories_read": reads.values("story_slug").distinct().count(),
            "reading_sessions": sessions,
            "reading_seconds": seconds,
            "average_reading_seconds": round(seconds / sessions, 1) if sessions else None,
            "over_time": fill_time_buckets(list(series), days, defaults={"visitors": 0, "page_views": 0, "reading_seconds": 0}),
            "top_stories": [{"slug": row["story_slug"], "title": row["story__title"] or row["story_slug"],
                             "readers": row["readers"], "reads": row["reads"], "reading_seconds": row["reading_seconds"]} for row in top],
        })
