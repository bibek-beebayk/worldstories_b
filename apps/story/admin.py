from django.contrib import admin
from .models import Audio, Story, Genre, Tag, Author, Chapter, Review


class ChapterInline(admin.StackedInline):
    model = Chapter
    extra = 0
    prepopulated_fields = {"slug": ("title",)}

class AudioInline(admin.StackedInline):
    model = Audio
    extra = 0


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    inlines = [ChapterInline, AudioInline]
    prepopulated_fields = {"slug": ("title",)}


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    pass


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    pass


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    pass


@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    pass


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("story", "user", "rating", "created_at")
    search_fields = ("story__title", "user__email", "comment")
