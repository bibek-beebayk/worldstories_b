from django.db import models
from django_ckeditor_5.fields import CKEditor5Field

STORY_TYPE_CHOICES = [
    ("Short Story", "Short Story"),
    ("Novel", "Novel"),
    ("Poetry", "Poetry"),
    ("Non Fiction", "Non Fiction"),
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

    def stories_count(self):
        return self.stories.count()

    def __str__(self):
        return self.name


class Story(models.Model):
    title = models.CharField(max_length=256)
    # content = models.TextField()
    slug = models.SlugField(max_length=256, unique=True)
    about = models.TextField(blank=True, null=True)
    genres = models.ManyToManyField(Genre, related_name="stories")
    story_type = models.CharField(
        max_length=50, choices=STORY_TYPE_CHOICES, default="Short Story"
    )
    author = models.ForeignKey(Author, on_delete=models.CASCADE, blank=True, null=True, related_name="stories")
    published_date = models.DateField(blank=True, null=True)
    cover_image = models.URLField(blank=True, null=True)
    is_completed = models.BooleanField(default=False)
    tags = models.ManyToManyField(Tag, blank=True)
    rating = models.FloatField(default=0.0)
    views = models.PositiveIntegerField(default=0)

    # def rating(self):
    #     # reviews = self.reviews.all()
    #     # if reviews.exists():
    #     #     return sum(review.rating for review in reviews) / reviews.count()
    #     return 4.7
    
    # def views(self):
    #     return 1234

    def has_audio(self):
        # TODO
        # return hasattr(self, 'audio')
        return self.audios.exists()

    def __str__(self):
        return self.title
    
    class Meta:
        verbose_name = "Story"
        verbose_name_plural = "Stories"


class Chapter(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="chapters")
    title = models.CharField(max_length=256)
    slug = models.SlugField(max_length=256, null=True)
    content = CKEditor5Field('Text', config_name='extends')
    order = models.PositiveIntegerField()

    class Meta:
        unique_together = ("story", "order")
        ordering = ["order"]

    def __str__(self):
        return f"{self.story.title} - Chapter {self.order}: {self.title}"
    

class Audio(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="audios")
    title = models.CharField(max_length=256)
    slug = models.SlugField(max_length=256, null=True)
    audio_file = models.FileField(upload_to='story_audios/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    order = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"Audio for {self.story.title} uploaded at {self.uploaded_at}"
    
    class Meta:
        unique_together = ("story", "order")
        ordering = ["order"]
