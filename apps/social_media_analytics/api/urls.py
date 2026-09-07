"""URL map for the social-media analytics API.

Included from the project's root ``urls.py`` under
``/api/social-media-analytics/`` so nothing here can collide with
Worldstories' own routes or with the existing ``apps.stats`` analytics
endpoints at ``/api/analytics/`` and ``/api/admin/analytics/``.
"""

from __future__ import annotations

from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .. import views as oauth_views
from . import views

app_name = "social_media_analytics"

connections = views.ConnectionViewSet
content = views.ContentViewSet

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),

    # Auth — simplejwt, the scheme the project already uses.
    path("auth/login/", TokenObtainPairView.as_view(), name="login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="refresh"),

    # Platform connections.
    path(
        "connections/",
        connections.as_view({"get": "list"}),
        name="connection-list",
    ),
    path(
        "connections/sync-status/",
        connections.as_view({"get": "sync_status"}),
        name="connection-sync-status",
    ),
    path(
        "connections/<str:platform>/oauth-url/",
        connections.as_view({"get": "oauth_url"}),
        name="connection-oauth-url",
    ),
    path(
        "connections/<str:platform>/disconnect/",
        connections.as_view({"post": "disconnect"}),
        name="connection-disconnect",
    ),

    # Content & metrics.
    path("content/", content.as_view({"get": "list"}), name="content-list"),
    path("content/<int:pk>/", content.as_view({"get": "retrieve"}), name="content-detail"),
    path(
        "content/<int:pk>/history/",
        content.as_view({"get": "history"}),
        name="content-history",
    ),

    # Dashboard aggregates.
    path(
        "dashboard/summary/",
        views.DashboardSummaryView.as_view(),
        name="dashboard-summary",
    ),
    path(
        "dashboard/account-trends/",
        views.AccountTrendsView.as_view(),
        name="dashboard-account-trends",
    ),

    # Manual "sync now" from the app.
    path("sync/", views.TriggerSyncView.as_view(), name="sync-now"),

    # OAuth redirect targets. These must match the redirect URIs registered
    # in each provider's developer console exactly.
    path(
        "oauth/<str:provider>/callback/",
        oauth_views.oauth_callback,
        name="oauth-callback",
    ),
]
