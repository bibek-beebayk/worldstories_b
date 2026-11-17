"""The recommended default homepage layout.

Applied only through `seed_recommended_layout()`, which does nothing when any
HomeSection already exists — an editor's own arrangement is never touched.
Sections for the other audience are hidden with `show_to`, so one list serves
both:

  signed out: hero → Featured → Originals → Trending → Quick Reads →
              Explore tiles → Watch → Blog → genre rails
  signed in:  compact hero → Continue → Recommended → Originals → Trending →
              Quick Reads → Explore tiles → Watch → Blog
"""

from django.db.models import Count

from .models import Genre, HomeSection

GENRE_RAILS = 3


def recommended_layout():
    """Section field dicts in page order."""
    sections = [
        dict(section_type="continue_shelf", title="Continue", show_to="signed_in"),
        dict(section_type="recommended", title="Recommended for You", icon="Sparkles", show_to="signed_in"),
        dict(
            section_type="stories_query", variant="originals", title="WorldStories Originals",
            subtitle="Exclusive stories published in-house for the WorldStories community.",
            layout="rail", item_limit=12, columns_desktop=6, see_all_url="/originals",
            config={"sort": "newest", "original_only": True, "require_cover": True},
        ),
        dict(
            section_type="stories_query", title="Trending now", icon="Flame",
            subtitle="What readers and listeners are into this week.",
            layout="trending", item_limit=5, allow_repeats=True, see_all_url="/library",
            config={"sort": "trending", "require_cover": True},
        ),
        dict(
            section_type="stories_query", variant="quick_read", title="Quick Reads", icon="Zap",
            item_limit=6, columns_mobile=2, columns_desktop=6, see_all_url="/quick-reads",
            config={"sort": "newest", "has_summary": True},
        ),
        dict(section_type="explore_tiles", title="Explore"),
        dict(
            section_type="videos", title="Watch & Listen", icon="Youtube", item_limit=4,
            columns_mobile=2, columns_desktop=4, see_all_url="/watch",
            config={"video_type": None, "standalone_only": False},
        ),
        dict(
            section_type="blogs", title="From the Blog", icon="Newspaper", item_limit=4,
            columns_mobile=2, columns_desktop=4, see_all_url="/blog",
        ),
    ]
    top_genres = (
        Genre.objects.annotate(n=Count("stories"))
        .filter(n__gt=0)
        .order_by("-n", "name")[:GENRE_RAILS]
    )
    for genre in top_genres:
        sections.append(
            dict(
                section_type="stories_query", title=genre.name, layout="rail", item_limit=12,
                columns_desktop=6, show_to="signed_out",
                config={"sort": "newest", "genres": [genre.pk], "require_cover": True},
            )
        )
    return sections


def seed_recommended_layout():
    """Create the recommended sections. Returns how many were created (0 when
    sections already exist)."""
    if HomeSection.objects.exists():
        return 0
    created = 0
    for position, values in enumerate(recommended_layout()):
        HomeSection.objects.create(position=position, **values)
        created += 1
    return created
