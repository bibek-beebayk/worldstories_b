from rest_framework import serializers
from .models import Story, Genre, Chapter, Tag, Author


class GenreSerializer(serializers.ModelSerializer):
    stories_count = serializers.SerializerMethodField()

    def get_stories_count(self, obj):
        return obj.stories.count()
    
    class Meta:
        model = Genre
        fields = ["id", "name", "stories_count"]


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name", "bio", "image"]


class StoryListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "story_type",
            "published_date",
            "cover_image",
            "rating",
            "views",
        ]


class ChapterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chapter
        fields = ["id", "title", "order", "content", "slug"]


class StoryDetailSerializer(serializers.ModelSerializer):
    genres = GenreSerializer(many=True, read_only=True)
    author = AuthorSerializer(read_only=True)
    chapter_count = serializers.SerializerMethodField()
    chapters = ChapterSerializer(many=True, read_only=True)

    def get_chapter_count(self, obj):
        return obj.chapters.count()

    class Meta:
        model = Story
        fields = [
            "id",
            "title",
            "slug",
            "about",
            "genres",
            "story_type",
            "author",
            "published_date",
            "cover_image",
            "is_completed",
            "tags",
            "rating",
            "views",
            "chapter_count",
            "chapters",
        ]
