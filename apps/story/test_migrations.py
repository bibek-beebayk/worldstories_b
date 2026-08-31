from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AudioTranscriptMigrationTests(TransactionTestCase):
    """Proves the additive migration leaves pre-existing audio data intact."""

    # This test changes the live test schema. Suppress post_migrate during
    # its teardown flush so later TransactionTestCase classes that restore
    # serialized migration data do not collide with recreated content types.
    serialized_rollback = True
    migrate_from = ("story", "0067_video")
    migrate_to = ("story", "0068_audio_transcript")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        StoryType = old_apps.get_model("story", "StoryType")
        Story = old_apps.get_model("story", "Story")
        Audio = old_apps.get_model("story", "Audio")
        story_type = StoryType.objects.create(name="Migration Test Type")
        story = Story.objects.create(
            title="Existing Story",
            slug="existing-story-before-transcript",
            story_type_id=story_type.pk,
        )
        self.audio_id = Audio.objects.create(
            story_id=story.pk,
            title="Existing Audio",
            slug="existing-audio-before-transcript",
            audio_file="story_audios/existing-before-transcript.mp3",
            order=1,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        # Restore the schema expected by the rest of the test suite even if
        # another migration is added after this test was written.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_audio_survives_with_blank_transcript(self):
        Audio = self.apps.get_model("story", "Audio")
        audio = Audio.objects.get(pk=self.audio_id)

        self.assertEqual(audio.transcript, "")
        self.assertEqual(audio.audio_file.name, "story_audios/existing-before-transcript.mp3")


class AudioTranscriptCueMigrationTests(TransactionTestCase):
    """The cue model is added by a plain CreateModel — proves it applies over an
    existing audio row without touching it, and that cues can be attached after."""

    serialized_rollback = True
    migrate_from = ("story", "0068_audio_transcript")
    migrate_to = ("story", "0069_audiotranscriptcue")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        StoryType = old_apps.get_model("story", "StoryType")
        Story = old_apps.get_model("story", "Story")
        Audio = old_apps.get_model("story", "Audio")
        story_type = StoryType.objects.create(name="Cue Migration Type")
        story = Story.objects.create(
            title="Existing Story", slug="existing-story-before-cues", story_type_id=story_type.pk
        )
        self.audio_id = Audio.objects.create(
            story_id=story.pk,
            title="Existing Audio",
            slug="existing-audio-before-cues",
            audio_file="story_audios/existing-before-cues.mp3",
            transcript="<p>Kept.</p>",
            order=1,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_audio_is_untouched_and_starts_with_no_cues(self):
        Audio = self.apps.get_model("story", "Audio")
        AudioTranscriptCue = self.apps.get_model("story", "AudioTranscriptCue")

        audio = Audio.objects.get(pk=self.audio_id)
        self.assertEqual(audio.transcript, "<p>Kept.</p>")
        self.assertEqual(audio.transcript_cues.count(), 0)

        AudioTranscriptCue.objects.create(
            audio_id=self.audio_id, order=1, start_ms=0, end_ms=1000, text="First"
        )
        self.assertEqual(audio.transcript_cues.count(), 1)
