from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.urls import include, path
from django.conf.urls.static import static
from django.http import HttpResponse
from django.utils.html import escape
import os

from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from apps.story import api as story_api
from apps.story import analytics_api as story_analytics_api
from apps.stats import views as stats_views
from apps.users import api as users_api
from apps.story.models import Author, Story, Blog

router = DefaultRouter()

router.register("stories", story_api.StoryViewSet, basename="story")
router.register("authors", story_api.AuthorViewSet, basename="author")
router.register("story-types", story_api.StoryTypeViewSet, basename="story-type")
router.register("submissions", story_api.SubmissionViewSet, basename="submission")
router.register("blog", story_api.BlogViewSet, basename="blog")
router.register("admin/stories", story_api.StoryAdminViewSet, basename="admin-story")
router.register("admin/chapters", story_api.ChapterAdminViewSet, basename="admin-chapter")
router.register("admin/audios", story_api.AudioAdminViewSet, basename="admin-audio")
router.register("admin/submissions", story_api.SubmissionAdminViewSet, basename="admin-submission")
router.register("admin/authors", story_api.AuthorAdminViewSet, basename="admin-author")
router.register("admin/categories", story_api.CategoryAdminViewSet, basename="admin-category")
router.register("admin/story-types", story_api.StoryTypeAdminViewSet, basename="admin-story-type")
router.register("admin/users", users_api.UserAdminViewSet, basename="admin-user")
router.register("admin/blog", story_api.BlogAdminViewSet, basename="admin-blog")
router.register("admin/story-queue", story_api.StoryQueueViewSet, basename="admin-story-queue")
router.register("auth", users_api.AuthenticationViewSet, basename="auth")


def sitemap(request):
    site_url = os.environ.get(
        "SITE_URL", "https://worldstories.net"
    ).rstrip("/")
    entries = [
        f"<url><loc>{escape(site_url + path)}</loc></url>"
        for path in [
            "/",
            "/library",
            "/discover",
            "/story-map",
            "/audiobooks",
            "/authors",
            "/contest",
            "/about",
            "/contact",
            "/privacy",
            "/terms",
        ]
    ]

    # AI-generated "Summary" entries (e.g. for in-copyright books we don't host
    # the full text of) are thin by design — kept off the sitemap so they're not
    # offered to search engines as representative site content.
    stories = (
        Story.objects.published()
        .exclude(story_type__name="Summary")
        .prefetch_related("chapters")
        .only("slug", "site_published_date")
    )
    for story in stories.iterator(chunk_size=2000):
        last_modified = (
            f"<lastmod>{story.site_published_date.isoformat()}</lastmod>"
            if story.site_published_date
            else ""
        )
        entries.append(
            f"<url><loc>{escape(f'{site_url}/story/{story.slug}')}</loc>"
            f"{last_modified}</url>"
        )
        for chapter in story.chapters.all():
            if not chapter.slug:
                continue
            entries.append(
                f"<url><loc>{escape(f'{site_url}/read/{story.slug}/{chapter.slug}')}</loc>"
                f"{last_modified}</url>"
            )

    blogs = Blog.objects.published().only("slug", "created_at")
    for blog in blogs.iterator(chunk_size=2000):
        entries.append(
            f"<url><loc>{escape(f'{site_url}/blog/{blog.slug}')}</loc>"
            f"<lastmod>{blog.created_at.date().isoformat()}</lastmod></url>"
        )

    authors = Author.objects.all().only("id")
    for author in authors.iterator():
        entries.append(
            f"<url><loc>{escape(f'{site_url}/authors/{author.id}')}</loc></url>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(entries)
        + "</urlset>"
    )
    return HttpResponse(xml, content_type="application/xml")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/session-auth/", include("rest_framework.urls")),
    path("api/", include(router.urls)),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/home/", story_api.HomeDataAPIView.as_view(), name="home-data"),
    path("api/trending/", story_api.TrendingDataAPIView.as_view(), name="trending-data"),
    path("api/originals/", story_api.OriginalsDataAPIView.as_view(), name="originals-data"),
    path("api/discover/", story_api.DiscoverDataAPIView.as_view(), name="discover-data"),
    path("api/story-map/", story_api.StoryMapAPIView.as_view(), name="story-map-data"),
    path(
        "api/analytics/events/",
        stats_views.AnalyticsEventCreateAPIView.as_view(),
        name="analytics-events",
    ),
    path("api/search/", story_api.SearchStoryAPIView.as_view(), name="search-data"),
    path("api/admin/overview/", story_api.AdminOverviewAPIView.as_view(), name="admin-overview"),
    path("api/admin/prompt-settings/", story_api.PromptSettingsAPIView.as_view(), name="admin-prompt-settings"),
    path(
        "api/admin/analytics/content/",
        story_analytics_api.AdminAnalyticsContentAPIView.as_view(),
        name="admin-analytics-content",
    ),
    path(
        "api/admin/analytics/engagement/",
        story_analytics_api.AdminAnalyticsEngagementAPIView.as_view(),
        name="admin-analytics-engagement",
    ),
    path(
        "api/admin/analytics/users/",
        story_analytics_api.AdminAnalyticsUsersAPIView.as_view(),
        name="admin-analytics-users",
    ),
    path(
        "api/admin/analytics/geography/",
        story_analytics_api.AdminAnalyticsGeographyAPIView.as_view(),
        name="admin-analytics-geography",
    ),
    path(
        "api/admin/analytics/export/",
        story_analytics_api.AdminAnalyticsExportAPIView.as_view(),
        name="admin-analytics-export",
    ),
    path(
        "api/admin/analytics/submissions/",
        story_analytics_api.AdminAnalyticsSubmissionsAPIView.as_view(),
        name="admin-analytics-submissions",
    ),
    path(
        "api/admin/analytics/audience/",
        story_analytics_api.AdminAnalyticsAudienceAPIView.as_view(),
        name="admin-analytics-audience",
    ),
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
    path(
        "api/file-reading-progress/<slug:story_slug>/<str:file_format>/",
        stats_views.FileReadingProgressAPIView.as_view(),
        name="file-reading-progress",
    ),
    path("api/genres/", story_api.GenreListAPIView.as_view(), name="genre-list"),
    path("api/categories/", story_api.CategoryListAPIView.as_view(), name="category-list"),
    path("api/library-shelves/", story_api.LibraryShelvesAPIView.as_view(), name="library-shelves"),
    path("api/sitemap.xml", sitemap, name="sitemap"),
    path("ckeditor5/", include("django_ckeditor_5.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
