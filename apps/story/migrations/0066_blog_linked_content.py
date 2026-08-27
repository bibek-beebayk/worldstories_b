from django.db import migrations, models


def copy_existing_linked_stories(apps, schema_editor):
    Blog = apps.get_model("story", "Blog")
    through_model = Blog.linked_stories.through
    through_model.objects.bulk_create(
        [
            through_model(blog_id=blog_id, story_id=story_id)
            for blog_id, story_id in Blog.objects.exclude(linked_story_id=None).values_list(
                "id", "linked_story_id"
            )
        ],
        ignore_conflicts=True,
    )


def restore_first_linked_story(apps, schema_editor):
    Blog = apps.get_model("story", "Blog")
    for blog in Blog.objects.prefetch_related("linked_stories"):
        story_id = blog.linked_stories.values_list("id", flat=True).first()
        if story_id:
            Blog.objects.filter(pk=blog.pk).update(linked_story_id=story_id)


class Migration(migrations.Migration):
    dependencies = [("story", "0065_genre_category_slug_nullable")]

    operations = [
        migrations.AddField(
            model_name="blog",
            name="linked_blogs",
            field=models.ManyToManyField(
                blank=True,
                help_text="Optional blog posts shown after this published post.",
                related_name="linked_from_blogs",
                symmetrical=False,
                to="story.blog",
            ),
        ),
        migrations.AddField(
            model_name="blog",
            name="linked_stories",
            field=models.ManyToManyField(
                blank=True,
                help_text="Optional stories shown after the published post.",
                related_name="linked_blog_posts",
                to="story.story",
            ),
        ),
        migrations.RunPython(copy_existing_linked_stories, restore_first_linked_story),
        migrations.RemoveField(model_name="blog", name="linked_story"),
    ]
