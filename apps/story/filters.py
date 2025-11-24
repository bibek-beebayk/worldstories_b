from django_filters import rest_framework as filters
from .models import Story

class StoryFilter(filters.FilterSet):
    status = filters.CharFilter(method="filter_status", label="Status")
    genres = filters.CharFilter(method="filter_genres", label="Genres")
    sort = filters.CharFilter(method="filter_sort", label="Sort")

    def filter_status(self, queryset, name, value):
        if value.lower() == "completed":
            return queryset.filter(is_completed=True)
        elif value.lower() == "ongoing":
            return queryset.filter(is_completed=False)
        return queryset
    
    def filter_genres(self, queryset, name, value):
        genre_ids = [genre.strip() for genre in value.split(",")]
        return queryset.filter(genres__id__in=genre_ids).distinct()
    
    def filter_sort(self, queryset, name, value):
        if value.lower() == "recent":
            return queryset.order_by("-published_date")
        # TODO: Fix sorting logic
        elif value.lower() == "popular":
            return queryset.order_by("published_date")
        elif value.lower() == "rating":
            return queryset.order_by("-rating")
        elif value.lower() == "views":
            return queryset.order_by("-views")
        return queryset

    class Meta:
        model = Story
        fields = ["status", "genres"]