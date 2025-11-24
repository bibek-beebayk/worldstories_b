from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.urls import include, path
from django.conf.urls.static import static

from rest_framework.routers import DefaultRouter
from apps.story import api as story_api

router = DefaultRouter()

router.register("stories", story_api.StoryViewSet, basename="story")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api-auth/", include("rest_framework.urls")),
    path("api/", include(router.urls)),
    path("api/genres/", story_api.GenreListAPIView.as_view(), name="genre-list"),
    path("ckeditor5/", include('django_ckeditor_5.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
