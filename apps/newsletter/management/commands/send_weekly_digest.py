import time
from datetime import datetime, time as date_time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.story.models import Story

from ...digest import build_digest
from ...models import DigestDelivery, DigestIssue, NewsletterSubscription
from ...services import (
    build_digest_message,
    get_week_featured_story,
    prepare_digest_context,
    render_email_template,
    select_digest_audience,
)

NON_DELIVERING_BACKENDS = {
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
}


class Command(BaseCommand):
    help = "Build and send the weekly WorldStories digest to today's cohort."

    def _active_backend_path(self):
        connection = get_connection()
        return f"{connection.__class__.__module__}.{connection.__class__.__qualname__}"

    def _validate_sending_config(self):
        if not settings.NEWSLETTER_FROM_EMAIL:
            raise CommandError("NEWSLETTER_FROM_EMAIL is required")
        backend_path = self._active_backend_path()
        if (
            backend_path in NON_DELIVERING_BACKENDS
            or settings.EMAIL_BACKEND in NON_DELIVERING_BACKENDS
        ):
            raise CommandError(
                f"Refusing to send digest with non-delivering email backend: {backend_path}"
            )
        return backend_path

    def _send_and_log(self, message, backend_path):
        recipient = ", ".join(message.to)
        self.stdout.write(
            f"Sending digest via {backend_path} from={message.from_email} to={recipient}"
        )
        sent_count = message.send()
        self.stdout.write(f"send() returned {sent_count}")
        if sent_count == 0:
            raise RuntimeError("Email backend reported 0 messages submitted")
        return getattr(message, "anymail_status", None)

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--to")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--story-slug")
        parser.add_argument("--since")
        parser.add_argument("--force", action="store_true")

    def _since(self, value, now, tz):
        if value:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError as error:
                raise CommandError("--since must use YYYY-MM-DD") from error
            return datetime.combine(parsed, date_time.min, tzinfo=tz)
        return now - timedelta(days=7)

    def handle(self, *args, **options):
        now = options.get("now") or timezone.now()
        tz = ZoneInfo(settings.NEWSLETTER_SEND_TIMEZONE)
        issue_date = now.astimezone(tz).date()
        since = self._since(options["since"], now, tz)
        story_slug = options["story_slug"]

        if not story_slug:
            week_story = get_week_featured_story(issue_date)
            if week_story:
                story_slug = week_story.slug

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
            since, subscription=test_subscription, story_slug=story_slug, now=now
        )
        subject = (
            f"New this week: {data['new_stories'][0]['title']}"
            if data["new_stories"]
            else "Your week in stories"
        )

        if options["dry_run"]:
            plan = select_digest_audience(
                now=now, limit=options["limit"], assign_missing=False
            )
            context = prepare_digest_context(data, issue_date, test_subscription)
            self.stdout.write(
                f"Weekday: {plan['today_weekday']} ({plan['weekday_name']})"
            )
            self.stdout.write(f"Overdue: {len(plan['overdue'])}")
            self.stdout.write(f"Due today: {len(plan['due_today'])}")
            self.stdout.write(f"Would send (after daily cap): {len(plan['to_send'])}")
            self.stdout.write(f"Left over: {plan['left_over']}")
            self.stdout.write(
                f"Digest plan: {len(data['new_stories'])} stories, {len(data['new_posts'])} posts; "
                f"spotlight={data['story_of_the_week']['title'] if data['story_of_the_week'] else 'none'}"
            )
            self.stdout.write(render_email_template("newsletter/digest.txt", context))
            return

        if options["to"]:
            backend_path = self._validate_sending_config()
            message = build_digest_message(
                subject, data, issue_date, options["to"], test_subscription
            )
            try:
                provider_status = self._send_and_log(message, backend_path)
            except RuntimeError as error:
                raise CommandError(str(error)) from error
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
            self.stdout.write("No new stories or posts; nothing new — nothing sent.")
            return
        if not settings.NEWSLETTER_POSTAL_ADDRESS:
            raise CommandError("NEWSLETTER_POSTAL_ADDRESS is required for a real send")
        backend_path = self._validate_sending_config()

        plan = select_digest_audience(
            now=now, limit=options["limit"], assign_missing=True
        )
        subscriptions = plan["to_send"]
        left_over = plan["left_over"]

        if not subscriptions:
            if left_over:
                self.stdout.write(
                    f"Daily cap reached ({plan['already_sent_today']}/{settings.NEWSLETTER_DAILY_SEND_LIMIT}); "
                    f"{left_over} subscriber(s) remain for the next run."
                )
            else:
                self.stdout.write("No subscribers due today; nothing sent.")
            return

        existing = (
            DigestIssue.objects.select_related("featured_story")
            .filter(issue_date=issue_date)
            .first()
        )
        issue = existing or DigestIssue.objects.create(
            issue_date=issue_date,
            subject=subject,
            featured_story_id=data["story_of_the_week_id"],
            started_at=now,
        )
        if existing:
            issue.subject = subject
            if not issue.featured_story_id and data["story_of_the_week_id"]:
                issue.featured_story_id = data["story_of_the_week_id"]
            issue.status = DigestIssue.STATUS_STARTED
            issue.finished_at = None
            issue.save(
                update_fields=["subject", "featured_story", "status", "finished_at"]
            )

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
                    now=now,
                )
                message = build_digest_message(
                    subject, personalized, issue_date, subscription.email, subscription
                )
                self._send_and_log(message, backend_path)
                sent_at = options.get("now") or timezone.now()
                DigestDelivery.objects.update_or_create(
                    issue=issue,
                    subscription=subscription,
                    defaults={
                        "status": DigestDelivery.STATUS_SENT,
                        "error": "",
                        "sent_at": sent_at,
                    },
                )
                subscription.last_digest_sent_at = sent_at
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
        issue.finished_at = options.get("now") or timezone.now()
        issue.save(
            update_fields=["sent_count", "failed_count", "status", "finished_at"]
        )
        self.stdout.write(
            f"Digest complete: {issue.sent_count} sent, {issue.failed_count} failed"
        )
        if left_over:
            self.stdout.write(
                f"Daily cap reached; {left_over} subscriber(s) remain for the next run."
            )
        if issue.failed_count:
            raise CommandError("One or more digest deliveries failed")
