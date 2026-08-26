import json

from rest_framework import serializers

from apps.story.models import Chapter, Audio, Story, Blog
from apps.stats.models import (
    AnalyticsEvent,
    ReadingProgress,
    AudioReadingProgress,
    BlogReadingProgress,
    FileReadingProgress,
)


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


class BlogReadingProgressSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogReadingProgress
        fields = ["progress", "updated_at"]


class BlogReadingProgressWriteSerializer(serializers.Serializer):
    progress = serializers.FloatField(min_value=0.0, max_value=1.0)


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


class AnalyticsEventWriteSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    event_type = serializers.ChoiceField(choices=AnalyticsEvent.EVENT_CHOICES)
    visitor_id = serializers.CharField(max_length=64)
    session_id = serializers.CharField(max_length=64, required=False, allow_blank=True)
    story_slug = serializers.SlugField(required=False, allow_blank=True)
    blog_slug = serializers.SlugField(required=False, allow_blank=True)
    duration_seconds = serializers.FloatField(required=False, min_value=0, max_value=86400)
    value = serializers.FloatField(required=False)
    metadata = serializers.JSONField(required=False)

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Metadata must be an object.")
        if len(json.dumps(value, ensure_ascii=False)) > 2048:
            raise serializers.ValidationError("Metadata is too large.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        story_slug = validated_data.pop("story_slug", "")
        story = None
        if story_slug:
            story = Story.objects.published().filter(slug=story_slug).first()
        blog_slug = validated_data.pop("blog_slug", "")
        blog = None
        if blog_slug:
            blog = Blog.objects.published().filter(slug=blog_slug).first()
        event, _ = AnalyticsEvent.objects.get_or_create(
            event_id=validated_data.pop("event_id"),
            defaults={
                **validated_data,
                "story": story,
                "blog": blog,
                "user": request.user if request.user.is_authenticated else None,
            },
        )
        return event
