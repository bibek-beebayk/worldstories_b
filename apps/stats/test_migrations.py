import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class StoryCompletionBackfillMigrationTests(TransactionTestCase):
    """Readers must not lose the stories they had already finished.

    The backfill replays both mechanisms StoryCompletion replaces: the progress
    rows (which the old query could only read for chapters) and the historical
    story-level `completion` analytics events (whose only deduplication lived
    in the reader's browser).
    """

    serialized_rollback = True
    migrate_from = ("stats", "0012_storycompletion")
    migrate_to = ("stats", "0013_backfill_story_completions")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        StoryType = old_apps.get_model("story", "StoryType")
        Story = old_apps.get_model("story", "Story")
        Chapter = old_apps.get_model("story", "Chapter")
        Audio = old_apps.get_model("story", "Audio")
        ChapterReadingProgress = old_apps.get_model("stats", "ChapterReadingProgress")
        AudioReadingProgress = old_apps.get_model("stats", "AudioReadingProgress")
        AnalyticsEvent = old_apps.get_model("stats", "AnalyticsEvent")

        # The concrete User model rather than the historical one: rewinding
        # the *stats* app leaves users at an older migration state whose model
        # is missing columns the real table has, and the reader here is only
        # scenery — nothing about this migration touches the users app.
        self.user_id = get_user_model().objects.create_user(
            email="backfill@example.com", username="backfill", password="test-password"
        ).pk
        story_type = StoryType.objects.create(name="Backfill Type")

        def make_story(slug):
            return Story.objects.create(title=slug, slug=slug, story_type_id=story_type.pk)

        # Finished by reading every chapter.
        self.finished_text = make_story("backfill-finished-text").pk
        for index in range(2):
            chapter = Chapter.objects.create(
                story_id=self.finished_text,
                title=f"C{index}",
                slug=f"backfill-text-{index}",
                content="<p>x</p>",
                order=index + 1,
            )
            ChapterReadingProgress.objects.create(
                user_id=self.user_id,
                story_id=self.finished_text,
                chapter_id=chapter.pk,
                progress=1.0,
            )

        # Half read — must not be backfilled.
        self.partial_text = make_story("backfill-partial-text").pk
        for index in range(2):
            chapter = Chapter.objects.create(
                story_id=self.partial_text,
                title=f"C{index}",
                slug=f"backfill-partial-{index}",
                content="<p>x</p>",
                order=index + 1,
            )
            ChapterReadingProgress.objects.create(
                user_id=self.user_id,
                story_id=self.partial_text,
                chapter_id=chapter.pk,
                progress=1.0 if index == 0 else 0.4,
            )

        # Finished by listening — invisible to the old chapter-averaging query.
        self.finished_audio = make_story("backfill-finished-audio").pk
        audio = Audio.objects.create(
            story_id=self.finished_audio,
            title="Track",
            slug="backfill-track",
            audio_file="story_audios/fake.mp3",
            order=1,
        )
        AudioReadingProgress.objects.create(
            user_id=self.user_id,
            story_id=self.finished_audio,
            audio_id=audio.pk,
            progress=1.0,
        )

        # Known only from the historical analytics event.
        self.event_only = make_story("backfill-event-only").pk
        AnalyticsEvent.objects.create(
            event_id=uuid.uuid4(),
            event_type="completion",
            user_id=self.user_id,
            story_id=self.event_only,
            visitor_id="legacy-visitor",
            metadata={"content_type": "story", "item_slug": "backfill-event-only"},
        )

        # A per-chapter completion event says an item finished, not a story.
        self.chapter_event_only = make_story("backfill-chapter-event").pk
        AnalyticsEvent.objects.create(
            event_id=uuid.uuid4(),
            event_type="completion",
            user_id=self.user_id,
            story_id=self.chapter_event_only,
            visitor_id="legacy-visitor",
            metadata={"content_type": "chapter", "item_slug": "some-chapter"},
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _completed_story_ids(self):
        StoryCompletion = self.apps.get_model("stats", "StoryCompletion")
        return set(
            StoryCompletion.objects.filter(user_id=self.user_id).values_list(
                "story_id", flat=True
            )
        )

    def test_it_backfills_every_way_a_story_could_already_be_finished(self):
        self.assertEqual(
            self._completed_story_ids(),
            {self.finished_text, self.finished_audio, self.event_only},
        )

    def test_it_does_not_invent_completions(self):
        completed = self._completed_story_ids()

        self.assertNotIn(self.partial_text, completed)
        # A finished chapter is not a finished story.
        self.assertNotIn(self.chapter_event_only, completed)

    def test_it_preserves_the_progress_rows_it_read(self):
        ChapterReadingProgress = self.apps.get_model("stats", "ChapterReadingProgress")
        AudioReadingProgress = self.apps.get_model("stats", "AudioReadingProgress")

        self.assertEqual(ChapterReadingProgress.objects.count(), 4)
        self.assertEqual(AudioReadingProgress.objects.count(), 1)

    def test_backfilled_rows_are_marked_as_such(self):
        StoryCompletion = self.apps.get_model("stats", "StoryCompletion")

        sources = set(StoryCompletion.objects.values_list("source", flat=True))

        # A historical row tells us the story was finished but not reliably on
        # which surface, so it says so rather than inventing one.
        self.assertEqual(sources, {"backfill"})
