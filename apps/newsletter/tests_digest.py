from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.stats.models import ChapterReadingProgress, ReadingProgress, StoryCompletion
from apps.story.models import Blog, Chapter, Story
from apps.users.models import User

from .digest import build_digest
from .models import DigestDelivery, DigestIssue, NewsletterSubscription
from .services import build_digest_message, send_message_or_raise

SETTINGS = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    NEWSLETTER_FROM_EMAIL="WorldStories <digest@example.com>",
    NEWSLETTER_REPLY_TO="reply@example.com",
    NEWSLETTER_SITE_URL="https://worldstories.net",
    NEWSLETTER_API_URL="https://api.worldstories.net",
    NEWSLETTER_POSTAL_ADDRESS="WorldStories, Kathmandu, Nepal",
    NEWSLETTER_SEND_DELAY_SECONDS=0,
)


@override_settings(**SETTINGS)
class DigestContentTests(TestCase):
    def story(self, index, **kwargs):
        defaults = {
            "title": f"Story {index}",
            "slug": f"story-{index}",
            "about": "A memorable story about journeys, choices, and finding a way home.",
            "is_published": True,
            "rating": index,
            "views": index * 10,
        }
        defaults.update(kwargs)
        return Story.objects.create(**defaults)

    def test_window_and_max_counts(self):
        since = timezone.now() - timedelta(days=7)
        for index in range(7):
            self.story(index)
        for index in range(3):
            Blog.objects.create(
                title=f"Post {index}",
                slug=f"post-{index}",
                content="Words",
                is_published=True,
            )
        digest = build_digest(since)
        self.assertEqual(len(digest["new_stories"]), 5)
        self.assertEqual(len(digest["new_posts"]), 2)

    def test_story_of_week_excludes_last_eight_picks(self):
        since = timezone.now() - timedelta(days=7)
        stories = [self.story(index) for index in range(10)]
        for offset, story in enumerate(stories[2:]):
            DigestIssue.objects.create(
                issue_date=timezone.localdate() - timedelta(days=offset + 1),
                subject="Old",
                featured_story=story,
                status=DigestIssue.STATUS_FINISHED,
                started_at=timezone.now() - timedelta(days=offset + 1),
            )
        digest = build_digest(since)
        self.assertEqual(digest["story_of_the_week_id"], stories[1].id)

    def test_personalized_block_requires_linked_user_with_fresh_incomplete_progress(
        self,
    ):
        user = User.objects.create_user(
            email="reader@example.com", username="reader", password="pw"
        )
        story = self.story(1)
        first = Chapter.objects.create(
            story=story, title="First", slug="first", order=1, content="One"
        )
        Chapter.objects.create(
            story=story, title="Second", slug="second", order=2, content="Two"
        )
        ReadingProgress.objects.create(
            user=user, story=story, chapter=first, progress=0.5
        )
        ChapterReadingProgress.objects.create(
            user=user, story=story, chapter=first, progress=0.5
        )
        subscription = NewsletterSubscription.objects.create(
            email=user.email,
            user=user,
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        personalized = build_digest(
            timezone.now() - timedelta(days=7), subscription=subscription
        )
        anonymous = build_digest(timezone.now() - timedelta(days=7))
        self.assertEqual(personalized["continue_reading"]["chapter_number"], 1)
        self.assertEqual(personalized["continue_reading"]["percent"], 25)
        self.assertIsNone(anonymous["continue_reading"])
        StoryCompletion.objects.create(
            user=user, story=story, source=StoryCompletion.SOURCE_CHAPTERS
        )
        self.assertIsNone(
            build_digest(timezone.now() - timedelta(days=7), subscription=subscription)[
                "continue_reading"
            ]
        )

    def test_rendered_html_has_unsubscribe_utm_and_only_absolute_links(self):
        story = self.story(1, cover_image="/media/story.jpg")
        subscription = NewsletterSubscription.objects.create(
            email="reader@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        data = build_digest(
            timezone.now() - timedelta(days=7), subscription=subscription
        )
        message = build_digest_message(
            "Subject", data, timezone.localdate(), subscription.email, subscription
        )
        html = message.alternatives[0][0]
        self.assertIn("/newsletter/unsubscribe?", html)
        self.assertIn("utm_source=digest", html)
        self.assertIn("https://worldstories.net/media/story.jpg", html)
        self.assertNotRegex(html, r'(?:href|src)="/(?!/)')
        self.assertIn("List-Unsubscribe", message.extra_headers)
        self.assertEqual(
            message.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click"
        )

    @override_settings(NEWSLETTER_API_URL="https://api.worldstories.net/api")
    def test_api_url_with_api_suffix_is_not_duplicated(self):
        subscription = NewsletterSubscription.objects.create(
            email="reader@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        message = build_digest_message(
            "Subject",
            build_digest(timezone.now() - timedelta(days=7)),
            timezone.localdate(),
            subscription.email,
            subscription,
        )
        header = message.extra_headers["List-Unsubscribe"]
        self.assertIn("https://api.worldstories.net/api/newsletter/", header)
        self.assertNotIn("/api/api/", header)

    def test_zero_send_result_is_an_error(self):
        message = build_digest_message(
            "Subject",
            build_digest(timezone.now() - timedelta(days=7)),
            timezone.localdate(),
            "reader@example.com",
        )
        with patch.object(message, "send", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "0 messages submitted"):
                send_message_or_raise(message)


@override_settings(**SETTINGS)
class DigestCommandTests(TestCase):
    def setUp(self):
        self.story = Story.objects.create(
            title="Fresh Story", slug="fresh-story", is_published=True
        )
        self.confirmed = NewsletterSubscription.objects.create(
            email="confirmed@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        self.pending = NewsletterSubscription.objects.create(
            email="pending@example.com", status=NewsletterSubscription.STATUS_PENDING
        )

    def test_dry_run_sends_nothing(self):
        output = StringIO()
        call_command("send_weekly_digest", "--dry-run", stdout=output)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("Digest plan", output.getvalue())

    def test_normal_run_sends_to_confirmed_only(self):
        call_command("send_weekly_digest")
        self.assertEqual(
            [message.to for message in mail.outbox], [[self.confirmed.email]]
        )
        self.assertEqual(DigestDelivery.objects.count(), 1)
        self.confirmed.refresh_from_db()
        self.assertIsNotNone(self.confirmed.last_digest_sent_at)

    def test_rerun_after_partial_failure_does_not_resend_success(self):
        second = NewsletterSubscription.objects.create(
            email="second@example.com",
            status=NewsletterSubscription.STATUS_CONFIRMED,
            confirm_token=None,
        )
        with patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=[1, RuntimeError("provider")],
        ):
            with self.assertRaises(CommandError):
                call_command("send_weekly_digest")
        issue = DigestIssue.objects.get()
        self.assertEqual(
            issue.deliveries.filter(status=DigestDelivery.STATUS_SENT).count(), 1
        )
        with patch(
            "django.core.mail.EmailMultiAlternatives.send", return_value=1
        ) as send:
            call_command("send_weekly_digest")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(
            issue.deliveries.filter(status=DigestDelivery.STATUS_SENT).count(), 2
        )

    def test_empty_digest_skips_without_issue(self):
        Story.objects.update(
            site_published_date=timezone.localdate() - timedelta(days=30)
        )
        output = StringIO()
        call_command(
            "send_weekly_digest",
            "--since",
            timezone.localdate().isoformat(),
            stdout=output,
        )
        self.assertEqual(DigestIssue.objects.count(), 0)
        self.assertIn("nothing sent", output.getvalue())

    @override_settings(NEWSLETTER_POSTAL_ADDRESS="")
    def test_real_send_refuses_without_postal_address(self):
        with self.assertRaises(CommandError):
            call_command("send_weekly_digest")

    def test_test_copy_leaves_delivery_state_untouched(self):
        before = self.confirmed.last_digest_sent_at
        call_command("send_weekly_digest", "--to", self.confirmed.email)
        self.confirmed.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.confirmed.last_digest_sent_at, before)
        self.assertEqual(DigestIssue.objects.count(), 0)
        self.assertEqual(DigestDelivery.objects.count(), 0)
