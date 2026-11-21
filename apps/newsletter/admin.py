from django.contrib import admin

from .models import DigestDelivery, DigestIssue, NewsletterSubscription


@admin.register(NewsletterSubscription)
class NewsletterSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "status",
        "source",
        "language",
        "confirmed_at",
        "last_digest_sent_at",
    )
    list_filter = ("status", "source", "language")
    search_fields = ("email", "user__email")
    readonly_fields = ("confirm_token", "confirmed_token_hash", "unsubscribe_token")


@admin.register(DigestIssue)
class DigestIssueAdmin(admin.ModelAdmin):
    list_display = (
        "issue_date",
        "subject",
        "status",
        "sent_count",
        "failed_count",
        "started_at",
        "finished_at",
    )
    list_filter = ("status",)
    search_fields = ("subject",)


@admin.register(DigestDelivery)
class DigestDeliveryAdmin(admin.ModelAdmin):
    list_display = ("issue", "subscription", "status", "sent_at")
    list_filter = ("status",)
    search_fields = ("subscription__email", "issue__subject")
