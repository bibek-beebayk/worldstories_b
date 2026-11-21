import time
from datetime import datetime, time as date_time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.story.models import Story

from ...digest import build_digest
from ...models import DigestDelivery, DigestIssue, NewsletterSubscription
from ...services import (
    build_digest_message,
    prepare_digest_context,
    render_email_template,
    send_message_or_raise,
)


class Command(BaseCommand):
    help = "Build and send the weekly WorldStories digest."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--to")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--story-slug")
        parser.add_argument("--since")
        parser.add_argument("--force", action="store_true")

    def _since(self, value):
        if value:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError as error:
                raise CommandError("--since must use YYYY-MM-DD") from error
            return timezone.make_aware(datetime.combine(parsed, date_time.min))
        previous = (
            DigestIssue.objects.filter(status=DigestIssue.STATUS_FINISHED)
            .order_by("-issue_date")
            .first()
        )
        return previous.started_at if previous else timezone.now() - timedelta(days=7)

    def handle(self, *args, **options):
        issue_date = timezone.localdate()
        since = self._since(options["since"])
        story_slug = options["story_slug"]
        existing = (
            DigestIssue.objects.select_related("featured_story")
            .filter(issue_date=issue_date)
            .first()
        )
        if (
            existing
            and existing.status == DigestIssue.STATUS_FINISHED
            and not options["force"]
            and not options["to"]
            and not options["dry_run"]
        ):
            raise CommandError("Today's digest is already finished")
        if existing and existing.featured_story and not story_slug:
            story_slug = existing.featured_story.slug
        if (
            story_slug
            and not Story.objects.published().filter(slug=story_slug).exists()
        ):
            raise CommandError("--story-slug must identify a published story")

        test_subscription = None
        if options["to"]:
            test_subscription = (
                NewsletterSubscription.objects.select_related("user")
                .filter(email__iexact=options["to"])
                .first()
            )
        data = build_digest(
            since, subscription=test_subscription, story_slug=story_slug
        )
        subject = (
            f"New this week: {data['new_stories'][0]['title']}"
            if data["new_stories"]
            else "Your week in stories"
        )

        if options["dry_run"]:
            context = prepare_digest_context(data, issue_date, test_subscription)
            self.stdout.write(
                f"Digest plan: {len(data['new_stories'])} stories, {len(data['new_posts'])} posts; "
                f"spotlight={data['story_of_the_week']['title'] if data['story_of_the_week'] else 'none'}"
            )
            self.stdout.write(render_email_template("newsletter/digest.txt", context))
            return

        if options["to"]:
            if not settings.NEWSLETTER_FROM_EMAIL:
                raise CommandError("NEWSLETTER_FROM_EMAIL is required")
            message = build_digest_message(
                subject, data, issue_date, options["to"], test_subscription
            )
            provider_status = send_message_or_raise(message)
            details = ""
            if provider_status:
                event = ",".join(sorted(provider_status.status or [])) or "submitted"
                details = (
                    f" (provider event: {event}; id: {provider_status.message_id})"
                )
            self.stdout.write(
                self.style.SUCCESS(f"Submitted test digest to {options['to']}{details}")
            )
            return

        if not data["new_stories"] and not data["new_posts"] and not options["force"]:
            self.stdout.write("No new stories or posts; nothing sent.")
            return
        if not settings.NEWSLETTER_POSTAL_ADDRESS or not settings.NEWSLETTER_FROM_EMAIL:
            raise CommandError(
                "NEWSLETTER_POSTAL_ADDRESS and NEWSLETTER_FROM_EMAIL are required for a real send"
            )

        issue = existing or DigestIssue.objects.create(
            issue_date=issue_date,
            subject=subject,
            featured_story_id=data["story_of_the_week_id"],
            started_at=timezone.now(),
        )
        if existing:
            issue.subject = subject
            issue.featured_story_id = data["story_of_the_week_id"]
            issue.status = DigestIssue.STATUS_STARTED
            issue.finished_at = None
            issue.save(
                update_fields=["subject", "featured_story", "status", "finished_at"]
            )

        audience = (
            NewsletterSubscription.objects.filter(
                status=NewsletterSubscription.STATUS_CONFIRMED, language="en"
            )
            .select_related("user")
            .order_by("id")
        )
        if options["limit"] is not None:
            audience = audience[: max(0, options["limit"])]

        subscriptions = list(audience)
        personalized_story_slug = (
            issue.featured_story.slug if issue.featured_story_id else story_slug
        )
        for index, subscription in enumerate(subscriptions):
            delivery = DigestDelivery.objects.filter(
                issue=issue, subscription=subscription
            ).first()
            if delivery and delivery.status == DigestDelivery.STATUS_SENT:
                continue
            try:
                personalized = build_digest(
                    since,
                    subscription=subscription,
                    story_slug=personalized_story_slug,
                )
                message = build_digest_message(
                    subject, personalized, issue_date, subscription.email, subscription
                )
                send_message_or_raise(message)
                DigestDelivery.objects.update_or_create(
                    issue=issue,
                    subscription=subscription,
                    defaults={
                        "status": DigestDelivery.STATUS_SENT,
                        "error": "",
                        "sent_at": timezone.now(),
                    },
                )
                subscription.last_digest_sent_at = timezone.now()
                subscription.save(update_fields=["last_digest_sent_at"])
            except Exception as error:  # one address must not stop the issue
                DigestDelivery.objects.update_or_create(
                    issue=issue,
                    subscription=subscription,
                    defaults={
                        "status": DigestDelivery.STATUS_FAILED,
                        "error": str(error),
                        "sent_at": None,
                    },
                )
                self.stderr.write(f"Failed {subscription.email}: {error}")
            if (
                index < len(subscriptions) - 1
                and settings.NEWSLETTER_SEND_DELAY_SECONDS
            ):
                time.sleep(settings.NEWSLETTER_SEND_DELAY_SECONDS)

        issue.sent_count = issue.deliveries.filter(
            status=DigestDelivery.STATUS_SENT
        ).count()
        issue.failed_count = issue.deliveries.filter(
            status=DigestDelivery.STATUS_FAILED
        ).count()
        issue.status = (
            DigestIssue.STATUS_FAILED
            if issue.failed_count
            else DigestIssue.STATUS_FINISHED
        )
        issue.finished_at = timezone.now()
        issue.save(
            update_fields=["sent_count", "failed_count", "status", "finished_at"]
        )
        self.stdout.write(
            f"Digest complete: {issue.sent_count} sent, {issue.failed_count} failed"
        )
        if issue.failed_count:
            raise CommandError("One or more digest deliveries failed")
