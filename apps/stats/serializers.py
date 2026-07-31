from rest_framework import serializers

from apps.story.models import Chapter, Audio
from apps.stats.models import ReadingProgress, AudioReadingProgress, FileReadingProgress


class ChapterProgressSerializer(serializers.Serializer):
    chapter_slug = serializers.CharField()
    progress = serializers.FloatField()


class ReadingProgressSerializer(serializers.ModelSerializer):
    chapter_slug = serializers.SerializerMethodField()
    overall_progress = serializers.SerializerMethodField()
    chapter_progresses = serializers.SerializerMethodField()

    class Meta:
        model = ReadingProgress
        fields = [
            "chapter_slug",
            "progress",
            "overall_progress",
            "chapter_progresses",
            "last_element_id",
            "updated_at",
        ]

    def get_chapter_slug(self, obj):
        return obj.chapter.slug if obj.chapter else None

    def get_overall_progress(self, obj):
        return self.context.get("overall_progress", 0.0)

    def get_chapter_progresses(self, obj):
        chapter_progress_map = self.context.get("chapter_progress_map", {})
        return [
            {"chapter_slug": chapter_slug, "progress": progress}
            for chapter_slug, progress in chapter_progress_map.items()
        ]


class ReadingProgressWriteSerializer(serializers.Serializer):
    chapter_slug = serializers.CharField(required=False, allow_blank=True)
    progress = serializers.FloatField(min_value=0.0, max_value=1.0)
    last_element_id = serializers.CharField(required=False, allow_blank=True)

    def validate_chapter_slug(self, value):
        return value.strip()

    def validate(self, attrs):
        story = self.context["story"]
        chapter_slug = attrs.get("chapter_slug")
        chapter = None
        if chapter_slug:
            chapter = Chapter.objects.filter(story=story, slug=chapter_slug).first()
            if not chapter:
                raise serializers.ValidationError(
                    {"chapter_slug": "Invalid chapter for this story."}
                )
        attrs["chapter"] = chapter
        return attrs


class FileReadingProgressSerializer(serializers.ModelSerializer):
    class Meta:
        model = FileReadingProgress
        fields = ["format", "progress", "position", "updated_at"]


class FileReadingProgressWriteSerializer(serializers.Serializer):
    progress = serializers.FloatField(min_value=0.0, max_value=1.0)
    position = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class AudioProgressSerializer(serializers.Serializer):
    audio_slug = serializers.CharField()
    progress = serializers.FloatField()
    position_seconds = serializers.FloatField()
    duration_seconds = serializers.FloatField()


class AudioReadingProgressSerializer(serializers.ModelSerializer):
    audio_slug = serializers.SerializerMethodField()
    overall_progress = serializers.SerializerMethodField()
    audio_progresses = serializers.SerializerMethodField()

    class Meta:
        model = AudioReadingProgress
        fields = [
            "audio_slug",
            "progress",
            "position_seconds",
            "duration_seconds",
            "overall_progress",
            "audio_progresses",
            "updated_at",
        ]

    def get_audio_slug(self, obj):
        return obj.audio.slug if obj.audio else None

    def get_overall_progress(self, obj):
        return self.context.get("overall_progress", 0.0)

    def get_audio_progresses(self, obj):
        audio_progress_map = self.context.get("audio_progress_map", {})
        return [
            {
                "audio_slug": audio_slug,
                "progress": values["progress"],
                "position_seconds": values["position_seconds"],
                "duration_seconds": values["duration_seconds"],
            }
            for audio_slug, values in audio_progress_map.items()
        ]


class AudioReadingProgressWriteSerializer(serializers.Serializer):
    audio_slug = serializers.CharField(required=False, allow_blank=True)
    progress = serializers.FloatField(min_value=0.0, max_value=1.0)
    position_seconds = serializers.FloatField(required=False, min_value=0.0, default=0.0)
    duration_seconds = serializers.FloatField(required=False, min_value=0.0, default=0.0)

    def validate_audio_slug(self, value):
        return value.strip()

    def validate(self, attrs):
        story = self.context["story"]
        audio_slug = attrs.get("audio_slug")
        audio = None
        if audio_slug:
            audio = Audio.objects.filter(story=story, slug=audio_slug).first()
            if not audio:
                raise serializers.ValidationError(
                    {"audio_slug": "Invalid audio for this story."}
                )
        attrs["audio"] = audio
        return attrs
