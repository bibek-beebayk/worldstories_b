from django.contrib import admin
from .models import Story, Genre, Tag, Author, Chapter


class ChapterInline(admin.StackedInline):
    model = Chapter
    extra = 0
    prepopulated_fields = {"slug": ("title",)}


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    inlines = [ChapterInline]
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
