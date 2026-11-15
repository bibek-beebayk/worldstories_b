from django.db.models import Count
from rest_framework import status
from rest_framework.filters import SearchFilter
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from apps.story.api import IsSuperUser

from .models import Feedback
from .serializers import AdminFeedbackSerializer, FeedbackCreateSerializer


class FeedbackThrottle(SimpleRateThrottle):
    """Keyed per account when signed in, per IP otherwise — the form is open
    to signed-out visitors, so this is what keeps it from becoming a spam inbox."""

    scope = "feedback"

    def get_rate(self):
        return "10/hour"

    def get_cache_key(self, request, view):
        if request.user.is_authenticated:
            ident = str(request.user.pk)
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class FeedbackCreateAPIView(APIView):
    permission_classes = [AllowAny]
    # JSON for text-only feedback, multipart when screenshots are attached.
    parser_classes = [JSONParser, MultiPartParser]
    throttle_classes = [FeedbackThrottle]

    def post(self, request):
        serializer = FeedbackCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user if request.user.is_authenticated else None
        serializer.save(
            user=user,
            email=serializer.validated_data.get("email") or (user.email if user else ""),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        )
        return Response({"detail": "Thanks for your feedback!"}, status=status.HTTP_201_CREATED)


class FeedbackAdminViewSet(ModelViewSet):
    """The feedback inbox in the React admin panel. Admins triage it (status +
    private notes) or delete spam; the feedback itself is read-only."""

    queryset = Feedback.objects.select_related("user").prefetch_related("images")
    serializer_class = AdminFeedbackSerializer
    permission_classes = [IsSuperUser]
    http_method_names = ["get", "patch", "delete", "head", "options"]
    filter_backends = [SearchFilter]
    search_fields = ["message", "email", "user__email", "user__username"]

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get("status")
        if status_filter in dict(Feedback.STATUS_CHOICES):
            queryset = queryset.filter(status=status_filter)
        category = self.request.query_params.get("category")
        if category in dict(Feedback.CATEGORY_CHOICES):
            queryset = queryset.filter(category=category)
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # Unfiltered per-status totals, for the inbox's filter labels and the
        # admin sidebar's "new" badge.
        counts = dict(
            Feedback.objects.order_by().values("status").annotate(n=Count("id")).values_list("status", "n")
        )
        response.data["status_counts"] = {
            value: counts.get(value, 0) for value, _label in Feedback.STATUS_CHOICES
        }
        return response
