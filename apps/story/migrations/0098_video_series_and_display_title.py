import django.db.models.deletion
from django.db import migrations, models


SERIES = (
    ("Under 2 Minute Stories", "under-2-minute-stories", "", 1, "tall", True),
    ("4-minute illustrated classics", "4-minute-illustrated-classics", "", 2, "wide", True),
    ("Full audiobooks & read-alongs", "full-audiobooks-read-alongs", "", 3, "wide", True),
    ("Folk tales & fables", "folk-tales-fables", "", 4, "wide", True),
    (
        "Curiosities",
        "curiosities",
        "Science explainers from our Amazed Mind channel",
        5,
        "wide",
        False,
    ),
)


def seed_and_assign(apps, schema_editor):
    Video = apps.get_model("story", "Video")
    VideoSeries = apps.get_model("story", "VideoSeries")
    by_slug = {}
    for name, slug, description, order, layout, is_story_content in SERIES:
        series, _ = VideoSeries.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "description": description,
                "order": order,
                "layout": layout,
                "is_story_content": is_story_content,
                "is_active": True,
            },
        )
        by_slug[slug] = series

    print("WorldStories video migration assignments:")
    for video in Video.objects.select_related("video_type").order_by("id"):
        derived = (video.title or "").split(" | ", 1)[0].strip()
        video.display_title = derived if derived else ""
        type_name = (video.video_type.name if video.video_type_id else "").strip().lower()
        series = None
        if video.aspect_ratio == "9:16":
            series = by_slug["under-2-minute-stories"]
        elif type_name in {"audiobook", "read along", "read-along"}:
            series = by_slug["full-audiobooks-read-alongs"]
        elif type_name == "book summary":
            series = by_slug["4-minute-illustrated-classics"]
        elif type_name == "animated narration":
            series = by_slug["folk-tales-fables"]
        elif type_name == "informational":
            series = by_slug["curiosities"]
        video.series_id = series.pk if series else None
        video.save(update_fields=["display_title", "series"])
        print(
            f"  #{video.pk} {video.title!r} -> display_title={video.display_title!r}; "
            f"series={series.name if series else 'Unassigned'}"
        )


def unassign(apps, schema_editor):
    Video = apps.get_model("story", "Video")
    VideoSeries = apps.get_model("story", "VideoSeries")
    Video.objects.update(series=None)
    VideoSeries.objects.filter(slug__in=[row[1] for row in SERIES]).delete()


class Migration(migrations.Migration):
    dependencies = [("story", "0097_seed_blog_topics_and_reading_times")]

    operations = [
        migrations.CreateModel(
            name="VideoSeries",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True)),
                ("slug", models.SlugField(max_length=140, unique=True)),
                ("description", models.TextField(blank=True, default="")),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("layout", models.CharField(choices=[("wide", "Wide"), ("tall", "Tall")], default="wide", max_length=8)),
                ("is_story_content", models.BooleanField(default=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"verbose_name_plural": "video series", "ordering": ["order", "name"]},
        ),
        migrations.AddField(
            model_name="video",
            name="display_title",
            field=models.CharField(blank=True, default="", max_length=256),
        ),
        migrations.AddField(
            model_name="video",
            name="series",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="videos", to="story.videoseries"),
        ),
        migrations.RunPython(seed_and_assign, unassign),
    ]
