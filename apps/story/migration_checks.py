from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AuthorSlugMigrationTests(TransactionTestCase):
    migrate_from = ("story", "0098_video_series_and_display_title")
    migrate_to = ("story", "0101_author_slug_unique")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        Author = old_apps.get_model("story", "Author")
        Story = old_apps.get_model("story", "Story")
        first = Author.objects.create(name="Frederick Douglass")
        second = Author.objects.create(name="Frederick Douglass")
        Story.objects.create(
            title="Narrative One",
            slug="narrative-one",
            author=first,
            is_published=True,
        )
        Story.objects.create(
            title="Narrative Two",
            slug="narrative-two",
            author=second,
            is_published=True,
        )
        self.author_ids = [first.pk, second.pk]

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_duplicate_names_get_unique_slugs_and_story_links_survive(self):
        Author = self.apps.get_model("story", "Author")
        Story = self.apps.get_model("story", "Story")

        authors = list(Author.objects.filter(pk__in=self.author_ids).order_by("pk"))
        self.assertEqual(
            [author.slug for author in authors],
            ["frederick-douglass", "frederick-douglass-2"],
        )
        self.assertEqual(
            set(Story.objects.filter(author_id__in=self.author_ids).values_list("author_id", flat=True)),
            set(self.author_ids),
        )
