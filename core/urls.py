from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.urls import include, path
from django.conf.urls.static import static

from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from apps.story import api as story_api
from apps.stats import views as stats_views
from apps.users import api as users_api

router = DefaultRouter()

router.register("stories", story_api.StoryViewSet, basename="story")
router.register("submissions", story_api.SubmissionViewSet, basename="submission")
router.register("admin/stories", story_api.StoryAdminViewSet, basename="admin-story")
router.register("admin/chapters", story_api.ChapterAdminViewSet, basename="admin-chapter")
router.register("admin/audios", story_api.AudioAdminViewSet, basename="admin-audio")
router.register("admin/submissions", story_api.SubmissionAdminViewSet, basename="admin-submission")
router.register("auth", users_api.AuthenticationViewSet, basename="auth")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/session-auth/", include("rest_framework.urls")),
    path("api/", include(router.urls)),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/home/", story_api.HomeDataAPIView.as_view(), name="home-data"),
    path("api/trending/", story_api.TrendingDataAPIView.as_view(), name="trending-data"),
    path("api/originals/", story_api.OriginalsDataAPIView.as_view(), name="originals-data"),
    path("api/discover/", story_api.DiscoverDataAPIView.as_view(), name="discover-data"),
    path("api/search/", story_api.SearchStoryAPIView.as_view(), name="search-data"),
    path("api/admin/overview/", story_api.AdminOverviewAPIView.as_view(), name="admin-overview"),
    path("api/admin/authors/", story_api.AdminAuthorListAPIView.as_view(), name="admin-authors"),
    path("api/admin/genres/", story_api.AdminGenreListCreateAPIView.as_view(), name="admin-genres"),
    path(
        "api/reading-progress/<slug:story_slug>/",
        stats_views.ReadingProgressAPIView.as_view(),
        name="reading-progress",
    ),
    path(
        "api/audio-progress/<slug:story_slug>/",
        stats_views.AudioReadingProgressAPIView.as_view(),
        name="audio-reading-progress",
    ),
    path("api/genres/", story_api.GenreListAPIView.as_view(), name="genre-list"),
    path("ckeditor5/", include("django_ckeditor_5.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
