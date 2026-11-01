from django.db import migrations

# The homepage body exactly as it was hardcoded in Index.tsx before sections
# became admin-managed, so the switch changes nothing visible.
SECTIONS = [
    dict(section_type="continue_shelf", title="Continue"),
    dict(section_type="reading_journey", title="Reading journey"),
    dict(section_type="new_entries", title="New Entries", icon="Rss"),
    dict(section_type="recommended", title="Recommended for You", icon="Sparkles"),
    dict(section_type="journeys_banner", title="Story journeys"),
    dict(
        section_type="stories_query", variant="quick_read", title="Quick Reads", icon="Zap",
        item_limit=6, columns_mobile=2, columns_desktop=6, see_all_url="/quick-reads",
        config={"sort": "newest", "has_summary": True},
    ),
    dict(
        section_type="videos", title="Watch", icon="Youtube", item_limit=4,
        columns_mobile=2, columns_desktop=4, see_all_url="/watch",
        config={"video_type": None, "standalone_only": False},
    ),
    dict(
        section_type="banner", title="Explore by Country", icon="Globe2",
        subtitle="Follow a story back to where it comes from — pick a country and start reading.",
        config={"button_label": "Open the Story Map", "button_url": "/story-map", "theme": "ocean"},
    ),
    dict(section_type="ad_slot", title="Ad", config={"ad_size": "banner"}),
    dict(
        section_type="blogs", title="From the Blog", icon="Newspaper", item_limit=4,
        columns_mobile=2, columns_desktop=4, see_all_url="/blog",
    ),
    dict(
        section_type="stories_query", title="Continue Discovering", item_limit=12,
        columns_mobile=2, columns_desktop=6, card_style="compact", see_all_url="/library",
        config={"sort": "recently_added"},
    ),
    dict(
        section_type="stories_query", variant="originals", title="WorldStories Originals",
        subtitle="Exclusive stories published in-house for the WorldStories community.",
        layout="rail", item_limit=12, columns_desktop=6, see_all_url="/originals",
        config={"sort": "newest", "original_only": True},
    ),
]


def seed(apps, schema_editor):
    HomeSection = apps.get_model("story", "HomeSection")
    if HomeSection.objects.exists():
        return
    for position, values in enumerate(SECTIONS):
        HomeSection.objects.create(position=position, **values)


class Migration(migrations.Migration):
    dependencies = [("story", "0092_homesection")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
