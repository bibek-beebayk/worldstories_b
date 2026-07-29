from django.db import models
from django_ckeditor_5.fields import CKEditor5Field
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings
from versatileimagefield.fields import VersatileImageField

STORY_TYPE_CHOICES = [
    ("Short Story", "Short Story"),
    ("Novel", "Novel"),
    ("Poetry", "Poetry"),
    ("Non Fiction", "Non Fiction"),
]

SUBMISSION_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("requires_edit", "Requires Edit"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
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
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="submitted_stories",
    )
    published_date = models.DateField(blank=True, null=True)
    cover_image = models.URLField(blank=True, null=True)
    cover_image_file = VersatileImageField(upload_to="story_covers/", blank=True, null=True)
    pdf_file = models.FileField(upload_to="story_files/pdfs/", blank=True, null=True)
    epub_file = models.FileField(upload_to="story_files/epubs/", blank=True, null=True)
    is_completed = models.BooleanField(default=False)
    tags = models.ManyToManyField(Tag, blank=True)
    rating = models.FloatField(default=0.0)
    views = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)

    def has_audio(self):
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


class Review(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="story_reviews"
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("story", "user")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.story.title} review by {self.user}"


class Favorite(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="favorites")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="story_favorites"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("story", "user")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} favorited {self.story}"


class Submission(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="story_submissions"
    )
    title = models.CharField(max_length=256)
    about = models.TextField()
    content = models.TextField()
    story_type = models.CharField(
        max_length=50, choices=STORY_TYPE_CHOICES, default="Short Story"
    )
    genres = models.ManyToManyField(Genre, related_name="submissions")
    cover_image = models.URLField(blank=True, null=True)
    cover_image_file = models.ImageField(
        upload_to="submission_covers/", blank=True, null=True
    )
    notes = models.TextField(blank=True, null=True)
    pdf_file = models.FileField(upload_to="submission_pdfs/", blank=True, null=True)
    epub_file = models.FileField(upload_to="submission_epubs/", blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=SUBMISSION_STATUS_CHOICES, default="pending"
    )
    reviewer_notes = models.TextField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="reviewed_submissions",
    )
    published_story = models.OneToOneField(
        "Story",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="submission",
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.status})"
