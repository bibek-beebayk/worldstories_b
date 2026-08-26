from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("story", "0062_tag_slug_and_storyqueue_tags"),
    ]

    operations = [
        migrations.CreateModel(
            name="Theme",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50)),
                ("slug", models.SlugField(blank=True, max_length=60, unique=True)),
                ("description", models.TextField(blank=True)),
            ],
        ),
        # No related_name was given when `tags` was first added, so its
        # reverse accessor defaulted to `tag.story_set` instead of the
        # `tag.stories` every Tag serializer/view/sitemap query assumes
        # (same shape as genres/categories, which do set this). ORM-only —
        # no DB schema/column change — but migration state must track it.
        migrations.AlterField(
            model_name="story",
            name="tags",
            field=models.ManyToManyField(blank=True, related_name="stories", to="story.tag"),
        ),
        migrations.AddField(
            model_name="story",
            name="themes",
            field=models.ManyToManyField(blank=True, related_name="stories", to="story.theme"),
        ),
        migrations.AddField(
            model_name="storyqueue",
            name="themes",
            field=models.ManyToManyField(blank=True, related_name="queue_items", to="story.theme"),
        ),
    ]
