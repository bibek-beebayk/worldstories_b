from django.db import models

STORY_TYPE_CHOICES = [
    ("Short Story", "Short Story"),
    ("Novel", "Novel"),
    ("Poetry", "Poetry"),
]

# STORY_STATUS_CHOICES = [
#     ("Draft", "Draft"),
#     ("Published", "Published"),
#     ("Archived", "Archived"),
# ]


class Genre(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class Author(models.Model):
    name = models.CharField(max_length=100)
    bio = models.TextField(blank=True, null=True)
    image = models.URLField(blank=True, null=True)

    def __str__(self):
        return self.name


class Story(models.Model):
    title = models.CharField(max_length=256)
    # content = models.TextField()
    slug = models.SlugField(max_length=256, unique=True)
    about = models.TextField(blank=True, null=True)
    genres = models.ManyToManyField(Genre)
    story_type = models.CharField(
        max_length=50, choices=STORY_TYPE_CHOICES, default="Short Story"
    )
    author = models.ForeignKey(Author, on_delete=models.CASCADE, blank=True, null=True)
    published_date = models.DateField(blank=True, null=True)
    cover_image = models.URLField(blank=True, null=True)
    is_completed = models.BooleanField(default=False)
    tags = models.ManyToManyField(Tag, blank=True)

    def __str__(self):
        return self.title


class Chapter(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="chapters")
    title = models.CharField(max_length=256)
    content = models.TextField()
    order = models.PositiveIntegerField()

    class Meta:
        unique_together = ("story", "order")
        ordering = ["order"]

    def __str__(self):
        return f"{self.story.title} - Chapter {self.order}: {self.title}"
