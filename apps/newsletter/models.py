import hashlib
import secrets

from django.conf import settings
from django.db import models


def newsletter_token():
    return secrets.token_urlsafe(32)


class NewsletterSubscription(models.Model):
    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_UNSUBSCRIBED = "unsubscribed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_UNSUBSCRIBED, "Unsubscribed"),
    ]
    SOURCE_CHOICES = [
        (value, value.title())
        for value in ("blog", "completion", "footer", "profile", "prompt", "other")
    ]

    email = models.EmailField(unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="newsletter_subscriptions",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default="other")
    language = models.CharField(max_length=10, default="en")
    confirm_token = models.CharField(
        max_length=64, unique=True, null=True, blank=True, default=""
    )
    confirmed_token_hash = models.CharField(
        max_length=64, blank=True, db_index=True, editable=False
    )
    unsubscribe_token = models.CharField(
        max_length=64, unique=True, default="", editable=False
    )
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    confirmation_send_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    unsubscribe_reason = models.CharField(max_length=255, blank=True)
    last_digest_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        if self.status == self.STATUS_PENDING and not self.confirm_token:
            self.confirm_token = newsletter_token()
        if not self.unsubscribe_token:
            self.unsubscribe_token = newsletter_token()
        super().save(*args, **kwargs)

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def __str__(self):
        return self.email


class DigestIssue(models.Model):
    STATUS_STARTED = "started"
    STATUS_FINISHED = "finished"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (value, value.title())
        for value in (STATUS_STARTED, STATUS_FINISHED, STATUS_FAILED)
    ]

    issue_date = models.DateField(unique=True)
    subject = models.CharField(max_length=255)
    featured_story = models.ForeignKey(
        "story.Story",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="newsletter_issues",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_STARTED
    )
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issue_date"]

    def __str__(self):
        return f"Digest {self.issue_date}"


class DigestDelivery(models.Model):
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [(STATUS_SENT, "Sent"), (STATUS_FAILED, "Failed")]

    issue = models.ForeignKey(
        DigestIssue, on_delete=models.CASCADE, related_name="deliveries"
    )
    subscription = models.ForeignKey(
        NewsletterSubscription, on_delete=models.CASCADE, related_name="deliveries"
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["issue", "subscription"], name="unique_digest_delivery"
            )
        ]

    def __str__(self):
        return f"{self.issue.issue_date}: {self.subscription.email} ({self.status})"
