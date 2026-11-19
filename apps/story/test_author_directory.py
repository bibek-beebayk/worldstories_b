import csv
import tempfile
from io import StringIO
from pathlib import Path

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.stats.models import AnalyticsEvent
from apps.story.models import Author, Genre, Story


class AuthorDirectoryApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.genre = Genre.objects.create(name="Fable", slug="fable")
        self.alpha = Author.objects.create(name="Alpha Writer", sort_name="Writer, Alpha", country="GB", era="1800s", known_for="Alpha Book")
        self.beta = Author.objects.create(name="Beta Writer", sort_name="Beta Writer", country="US", era="1900s")
        self.empty = Author.objects.create(name="Empty Writer")
        self.house = Author.objects.create(name="House Writer", is_house=True)
        self.alpha_story = Story.objects.create(title="Alpha", slug="alpha-story", author=self.alpha, is_published=True)
        self.alpha_story.genres.add(self.genre)
        self.beta_story = Story.objects.create(title="Beta", slug="beta-story", author=self.beta, is_published=True)
        Story.objects.create(title="House", slug="house-story", author=self.house, is_published=True)

    def results(self, **params):
        return self.client.get("/api/authors/", {"page_size": "all", **params}).data["results"]

    def test_all_page_size_uses_capped_directory_size(self):
        response = self.client.get("/api/authors/", {"page_size": "all"})
        self.assertEqual(response.data["pagination"]["size"], 500)

    def test_default_excludes_empty_and_house(self):
        self.assertEqual({row["id"] for row in self.results()}, {self.alpha.id, self.beta.id})
        included = self.results(include_empty="1")
        self.assertEqual({row["id"] for row in included}, {self.alpha.id, self.beta.id, self.empty.id, self.house.id})

    def test_filters_search_letter_country_era_and_genre(self):
        self.assertEqual([row["id"] for row in self.results(q="alpha book")], [self.alpha.id])
        self.assertEqual([row["id"] for row in self.results(letter="W")], [self.alpha.id])
        self.assertEqual([row["id"] for row in self.results(country="US")], [self.beta.id])
        self.assertEqual([row["id"] for row in self.results(era="1800s")], [self.alpha.id])
        self.assertEqual([row["id"] for row in self.results(genre="fable")], [self.alpha.id])

    def test_titles_and_popular_sorts_exclude_staff_and_bots(self):
        Story.objects.create(title="Alpha Two", slug="alpha-two", author=self.alpha, is_published=True)
        AnalyticsEvent.objects.create(event_type="reading_session", visitor_id="reader", story=self.beta_story)
        AnalyticsEvent.objects.create(event_type="reading_session", visitor_id="bot", story=self.alpha_story, is_suspected_bot=True)
        self.assertEqual(self.results(sort="titles")[0]["id"], self.alpha.id)
        self.assertEqual(self.results(sort="popular")[0]["id"], self.beta.id)

    def test_overview_shape_and_slug_detail(self):
        response = self.client.get("/api/authors/overview/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("featured", response.data)
        self.assertIn("letters", response.data)
        self.assertIn("countries", response.data)
        self.assertIn("eras", response.data)
        detail = self.client.get(f"/api/authors/{self.alpha.slug}/")
        self.assertEqual(detail.data["id"], self.alpha.id)


class AuthorBackfillCommandTests(TestCase):
    headers = ["current_name", "display_name", "sort_name", "current_titles", "birth_year", "death_year", "years_label", "country_code", "country", "era", "known_for", "is_traditional", "is_house", "needs_portrait", "action", "notes"]

    def csv_path(self, rows):
        handle = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".csv", delete=False)
        handle.close()
        path = Path(handle.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.headers)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path

    def row(self, **overrides):
        row = {field: "" for field in self.headers}
        row.update({"current_name": "Writer", "display_name": "Writer", "current_titles": "0", "action": "keep"})
        row.update(overrides)
        return row

    def test_dry_run_writes_nothing_and_blank_does_not_overwrite(self):
        author = Author.objects.create(name="Writer", bio="Keep me", years_label="Existing years")
        path = self.csv_path([self.row(sort_name="Writer, A", years_label="")])
        call_command("import_author_backfill", str(path), stdout=StringIO())
        author.refresh_from_db()
        self.assertEqual(author.sort_name, "")
        self.assertEqual(author.bio, "Keep me")
        self.assertEqual(author.years_label, "Existing years")
        call_command("import_author_backfill", str(path), apply=True, stdout=StringIO())
        author.refresh_from_db()
        self.assertEqual(author.sort_name, "Writer, A")
        self.assertEqual(author.bio, "Keep me")
        self.assertEqual(author.years_label, "Existing years")

    def test_duplicate_disambiguation_merge_moves_stories_and_is_idempotent(self):
        source = Author.objects.create(name="Frederick Douglass")
        target = Author.objects.create(name="Frederick Douglass")
        source_story = Story.objects.create(
            title="Narrative", slug="narrative", author=source, is_published=True
        )
        Story.objects.create(title="Speech", slug="speech", author=target, is_published=True)
        Story.objects.create(title="Memoir", slug="memoir", author=target, is_published=True)
        rows = [
            self.row(current_name="Frederick Douglass", display_name="Frederick Douglass", current_titles="1", action="merge"),
            self.row(current_name="Frederick Douglass", display_name="Frederick Douglass", current_titles="2", action="keep", sort_name="Douglass, Frederick"),
        ]
        path = self.csv_path(rows)
        call_command("import_author_backfill", str(path), apply=True, stdout=StringIO())
        self.assertFalse(Author.objects.filter(pk=source.pk).exists())
        source_story.refresh_from_db()
        self.assertEqual(source_story.author_id, target.id)
        self.assertEqual(target.stories.count(), 3)
        call_command("import_author_backfill", str(path), apply=True, stdout=StringIO())
        self.assertEqual(Author.objects.filter(name="Frederick Douglass").count(), 1)
