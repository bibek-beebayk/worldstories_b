"""App-level URL entry point — delegates to ``api/urls.py``."""

from .api.urls import urlpatterns  # noqa: F401

app_name = "social_media_analytics"
