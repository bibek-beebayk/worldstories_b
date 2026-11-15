from django.conf import settings
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models

from core.libs.validators import FileSizeValidator


class Feedback(models.Model):
    """Feedback about the app itself, sent from the site's Feedback button.

    Anyone can send it, signed in or not. `user` is filled from the request
    when there is one; `email` is an optional reply-to for signed-out senders
    (and defaults to the account's email for signed-in ones).
    """

    CATEGORY_BUG = "bug"
    CATEGORY_IDEA = "idea"
    CATEGORY_CONTENT = "content"
    CATEGORY_PRAISE = "praise"
    CATEGORY_OTHER = "other"
    CATEGORY_CHOICES = [
        (CATEGORY_BUG, "Bug / problem"),
        (CATEGORY_IDEA, "Idea / suggestion"),
        (CATEGORY_CONTENT, "Stories & content"),
        (CATEGORY_PRAISE, "Praise"),
        (CATEGORY_OTHER, "Other"),
    ]

    STATUS_NEW = "new"
    STATUS_REVIEWED = "reviewed"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback",
    )
    email = models.EmailField(blank=True)
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    message = models.TextField()
    # Where the sender was when they opened the form, so a bug report comes
    # with the page it's about.
    page_url = models.CharField(max_length=500, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    admin_notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Feedback"

    def __str__(self):
        return f"{self.get_category_display()}: {self.message[:60]}"


MAX_FEEDBACK_IMAGES = 5
MAX_FEEDBACK_IMAGE_BYTES = 5 * 1024 * 1024


class FeedbackImage(models.Model):
    """A screenshot attached to a piece of feedback (up to MAX_FEEDBACK_IMAGES each)."""

    feedback = models.ForeignKey(Feedback, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(
        upload_to="feedback/",
        validators=[
            FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "webp", "gif"]),
            FileSizeValidator(MAX_FEEDBACK_IMAGE_BYTES),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.image.name
