from django_filters import rest_framework as filters
from django.db.models import Q
from .models import Story, StoryMood

def public_story_mood_q():
    """Mood assignments a reader may see: an administrator's choice, or a
    machine suggestion someone has since confirmed. See StoryMood.is_public,
    which this mirrors at query level."""
    return Q(story_moods__source=StoryMood.SOURCE_ADMIN) | Q(story_moods__reviewed=True)


class StoryFilter(filters.FilterSet):
    status = filters.CharFilter(method="filter_status", label="Status")
    genres = filters.CharFilter(method="filter_genres", label="Genres")
    categories = filters.CharFilter(method="filter_categories", label="Categories")
    language = filters.CharFilter(method="filter_language", label="Language")
    story_type = filters.CharFilter(method="filter_story_type", label="Story type")
    country = filters.CharFilter(method="filter_country", label="Country")
    has_audio = filters.CharFilter(method="filter_has_audio", label="Has audio")
    has_video = filters.CharFilter(method="filter_has_video", label="Has video")
    has_summary = filters.CharFilter(method="filter_has_summary", label="Has summary")
    is_original = filters.CharFilter(method="filter_is_original", label="WorldStories Original")
    moods = filters.CharFilter(method="filter_moods", label="Moods")
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

    def filter_moods(self, queryset, name, value):
        """Filter by mood slug, ignoring assignments nobody has reviewed.

        An AI suggestion is stored but is not a fact about the story until an
        administrator has confirmed it (§8.5) — so it must not steer what a
        reader is shown. `public_story_mood_q()` is the single definition of
        that rule; do not re-express it inline anywhere.
        """
        slugs = [slug.strip() for slug in value.split(",") if slug.strip()]
        if not slugs:
            return queryset
        return queryset.filter(
            public_story_mood_q(), story_moods__mood__slug__in=slugs
        ).distinct()

    def filter_language(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(language=value)

    def filter_story_type(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(story_type_id=value)

    def filter_country(self, queryset, name, value):
        if not value or value.lower() == "all":
            return queryset
        return queryset.filter(country=value.upper())

    def filter_has_audio(self, queryset, name, value):
        if value.lower() == "true":
            return queryset.filter(audios__isnull=False).distinct()
        if value.lower() == "false":
            return queryset.filter(audios__isnull=True)
        return queryset

    def filter_has_video(self, queryset, name, value):
        if value.lower() == "true":
            return queryset.filter(videos__isnull=False).distinct()
        if value.lower() == "false":
            return queryset.filter(videos__isnull=True)
        return queryset

    def filter_has_summary(self, queryset, name, value):
        no_summary = Q(summary__isnull=True) | Q(summary__exact="")
        if value.lower() == "true":
            return queryset.exclude(no_summary)
        if value.lower() == "false":
            return queryset.filter(no_summary)
        return queryset

    def filter_is_original(self, queryset, name, value):
        if value.lower() == "true":
            return queryset.filter(is_original=True)
        if value.lower() == "false":
            return queryset.filter(is_original=False)
        return queryset

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
        fields = ["status", "genres", "categories", "language", "story_type", "country", "has_audio", "has_video", "has_summary", "is_original", "sort", "q"]
