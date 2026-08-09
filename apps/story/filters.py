from django_filters import rest_framework as filters
from django.db.models import Q
from .models import Story

class StoryFilter(filters.FilterSet):
    status = filters.CharFilter(method="filter_status", label="Status")
    genres = filters.CharFilter(method="filter_genres", label="Genres")
    categories = filters.CharFilter(method="filter_categories", label="Categories")
    language = filters.CharFilter(method="filter_language", label="Language")
    story_type = filters.CharFilter(method="filter_story_type", label="Story type")
    sort = filters.CharFilter(method="filter_sort", label="Sort")
    q = filters.CharFilter(method="filter_q", label="Query")

    def filter_status(self, queryset, name, value):
        if value.lower() == "completed":
            return queryset.filter(is_completed=True)
        elif value.lower() == "ongoing":
            return queryset.filter(is_completed=False)
        return queryset

    def filter_genres(self, queryset, name, value):
        genre_ids = [genre.strip() for genre in value.split(",")]
        return queryset.filter(genres__id__in=genre_ids).distinct()

    def filter_categories(self, queryset, name, value):
        category_ids = [category.strip() for category in value.split(",")]
        return queryset.filter(categories__id__in=category_ids).distinct()

    def filter_language(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(language=value)

    def filter_story_type(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(story_type=value)
    
    def filter_sort(self, queryset, name, value):
        if value.lower() == "recent":
            return queryset.order_by("-site_published_date")
        # TODO: Fix sorting logic
        elif value.lower() == "popular":
            return queryset.order_by("site_published_date")
        elif value.lower() == "rating":
            return queryset.order_by("-rating")
        elif value.lower() == "views":
            return queryset.order_by("-views")
        return queryset

    def filter_q(self, queryset, name, value):
        query = (value or "").strip()
        if not query:
            return queryset
        return queryset.filter(
            Q(title__icontains=query)
            | Q(about__icontains=query)
            | Q(author__name__icontains=query)
            | Q(genres__name__icontains=query)
            | Q(categories__name__icontains=query)
            | Q(tags__name__icontains=query)
        ).distinct()

    class Meta:
        model = Story
        fields = ["status", "genres", "categories", "language", "story_type", "sort", "q"]
