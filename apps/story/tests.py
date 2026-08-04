from datetime import date, datetime, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.story.api import StoryViewSet
from apps.story.models import Author, Story
from apps.story.serializers import StoryAdminSerializer
from core.urls import sitemap


class ScheduledPublishingTests(SimpleTestCase):
    @patch("apps.story.api.Story.objects.published")
    def test_public_queryset_is_built_for_each_request(self, published):
        queryset = MagicMock()
        queryset.order_by.return_value = queryset
        published.return_value = queryset
        view = StoryViewSet()
        view.action = "retrieve"

        self.assertIs(view.get_queryset(), queryset)
        published.assert_called_once_with()

    @patch("django.db.models.Model.save")
    def test_scheduled_story_uses_schedule_date_as_site_date(self, model_save):
        story = Story(
            title="Scheduled",
            slug="scheduled",
            is_published=True,
            publish_at=datetime(2026, 9, 4, 10, 0, tzinfo=datetime_timezone.utc),
        )

        story.save()

        self.assertEqual(story.site_published_date, date(2026, 9, 4))
        model_save.assert_called_once()

    @patch("core.urls.Author.objects.all")
    @patch("core.urls.Story.objects.published")
    def test_sitemap_uses_scheduled_publication_gate(self, published, authors_all):
        queryset = MagicMock()
        queryset.only.return_value.iterator.return_value = iter(
            [SimpleNamespace(slug="visible-story", site_published_date=date(2026, 8, 2))]
        )
        published.return_value = queryset
        authors_all.return_value.only.return_value.iterator.return_value = iter([])

        response = sitemap(RequestFactory().get("/api/sitemap.xml"))
        xml = response.content.decode()

        self.assertContains(response, "/story/visible-story")
        self.assertIn("<lastmod>2026-08-02</lastmod>", xml)
        published.assert_called_once_with()


class PublicAuthorApiTests(APITestCase):
    def setUp(self):
        self.visible_author = Author.objects.create(
            name="Visible Writer",
            bio="Writes public stories.",
            image="https://example.com/writer.jpg",
        )
        self.hidden_author = Author.objects.create(name="Draft Writer")
        Story.objects.create(
            title="Published Book",
            slug="published-book",
            author=self.visible_author,
            is_published=True,
        )
        Story.objects.create(
            title="Draft Book",
            slug="draft-book",
            author=self.visible_author,
            is_published=False,
        )
        Story.objects.create(
            title="Hidden Author Draft",
            slug="hidden-author-draft",
            author=self.hidden_author,
            is_published=False,
        )

    def test_list_includes_all_authors_and_only_counts_published_stories(self):
        response = self.client.get(reverse("author-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["pagination"]["count"], 2)
        counts = {author["id"]: author["stories_count"] for author in response.data["results"]}
        self.assertEqual(counts[self.visible_author.id], 1)
        self.assertEqual(counts[self.hidden_author.id], 0)

    def test_detail_only_includes_published_stories(self):
        response = self.client.get(reverse("author-detail", args=[self.visible_author.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([story["slug"] for story in response.data["stories"]], ["published-book"])

    def test_author_without_public_stories_has_an_empty_book_list(self):
        response = self.client.get(reverse("author-detail", args=[self.hidden_author.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stories_count"], 0)
        self.assertEqual(response.data["stories"], [])


class OriginalPublicationDateValidationTests(SimpleTestCase):
    def test_rejects_impossible_calendar_date(self):
        serializer = StoryAdminSerializer()
        with self.assertRaises(ValidationError):
            serializer.validate(
                {
                    "title": "Invalid date",
                    "slug": "invalid-date",
                    "original_published_year": 2025,
                    "original_published_month": 2,
                    "original_published_day": 31,
                }
            )

    def test_accepts_partial_original_publication_date(self):
        serializer = StoryAdminSerializer()
        attrs = {
            "title": "Year only",
            "slug": "year-only",
            "original_published_year": 1920,
            "original_published_month": None,
            "original_published_day": None,
        }
        self.assertEqual(serializer.validate(attrs), attrs)
