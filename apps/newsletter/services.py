from datetime import datetime, time as date_time, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Count, F, Q
from django.template import Context, Engine
from django.utils import timezone

from .models import DigestDelivery, DigestIssue, NewsletterSubscription

WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
WEEKDAY_PRIORITY = [5, 6, 4, 3, 2, 1, 0]


def _weekday_counts():
    counts = {day: 0 for day in range(7)}
    rows = (
        NewsletterSubscription.objects.filter(
            status=NewsletterSubscription.STATUS_CONFIRMED,
            send_weekday__isnull=False,
        )
        .values("send_weekday")
        .annotate(total=Count("id"))
    )
    for row in rows:
        day = row["send_weekday"]
        if day in counts:
            counts[day] = row["total"]
    return counts


def assign_send_weekday():
    counts = _weekday_counts()
    return min(WEEKDAY_PRIORITY, key=lambda day: counts[day])


def get_week_featured_story(today_date):
    week_start = today_date - timedelta(days=today_date.weekday())
    week_end = week_start + timedelta(days=6)
    issue = (
        DigestIssue.objects.select_related("featured_story")
        .filter(
            issue_date__gte=week_start,
            issue_date__lte=week_end,
            featured_story__isnull=False,
        )
        .order_by("issue_date")
        .first()
    )
    return issue.featured_story if issue else None


def select_digest_audience(now=None, limit=None, assign_missing=True):
    tz = ZoneInfo(settings.NEWSLETTER_SEND_TIMEZONE)
    now = now or timezone.now()
    local_now = now.astimezone(tz)
    today_date = local_now.date()
    today_weekday = local_now.weekday()

    confirmed = NewsletterSubscription.objects.filter(
        status=NewsletterSubscription.STATUS_CONFIRMED, language="en"
    ).select_related("user")

    simulated_today_ids = []
    if assign_missing:
        for sub in confirmed.filter(send_weekday__isnull=True):
            sub.send_weekday = assign_send_weekday()
            sub.save(update_fields=["send_weekday"])
    else:
        counts = _weekday_counts()
        for sub in confirmed.filter(send_weekday__isnull=True):
            chosen = min(WEEKDAY_PRIORITY, key=lambda day: counts[day])
            counts[chosen] += 1
            if chosen == today_weekday:
                simulated_today_ids.append(sub.pk)

    six_days_ago = now - timedelta(days=6)
    eight_days_ago = now - timedelta(days=8)
    overdue = confirmed.filter(
        Q(last_digest_sent_at__lte=eight_days_ago)
        | Q(last_digest_sent_at__isnull=True, confirmed_at__lte=eight_days_ago)
    ).order_by(F("last_digest_sent_at").asc(nulls_first=True))

    weekday_q = Q(send_weekday=today_weekday)
    if simulated_today_ids:
        weekday_q |= Q(pk__in=simulated_today_ids)

    due_today = (
        confirmed.filter(weekday_q)
        .filter(
            Q(last_digest_sent_at__isnull=True)
            | Q(last_digest_sent_at__lte=six_days_ago)
        )
        .exclude(pk__in=overdue.values("pk"))
    )

    overdue_list = list(overdue)
    due_today_list = list(due_today)
    audience = overdue_list + due_today_list

    day_start = datetime.combine(today_date, date_time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    already_sent_today = DigestDelivery.objects.filter(
        status=DigestDelivery.STATUS_SENT,
        sent_at__gte=day_start,
        sent_at__lt=day_end,
    ).count()
    allowed = max(0, settings.NEWSLETTER_DAILY_SEND_LIMIT - already_sent_today)
    if limit is not None:
        allowed = min(allowed, max(0, limit))

    to_send = audience[:allowed]
    left_over = max(0, len(audience) - len(to_send))

    return {
        "now": now,
        "tz": tz,
        "today_date": today_date,
        "today_weekday": today_weekday,
        "weekday_name": WEEKDAY_NAMES[today_weekday],
        "overdue": overdue_list,
        "due_today": due_today_list,
        "audience": audience,
        "already_sent_today": already_sent_today,
        "allowed": allowed,
        "to_send": to_send,
        "left_over": left_over,
    }


class EmailTemplateContext(Context):
    """Context with a Python 3.15-safe shallow copy for test instrumentation.

    Django 5.0's BaseContext uses ``copy(super())``, which Python 3.15 no
    longer permits. The project currently runs that combination locally; this
    keeps email templates renderable without changing global Django behavior.
    """

    def __copy__(self):
        duplicate = type(self)(
            autoescape=self.autoescape,
            use_l10n=self.use_l10n,
            use_tz=self.use_tz,
        )
        duplicate.dicts = self.dicts[:]
        duplicate.template_name = self.template_name
        duplicate.template = self.template
        duplicate.render_context.dicts = self.render_context.dicts[:]
        return duplicate


def render_email_template(name, context):
    # Render through the core template engine. This is equivalent to
    # render_to_string for app templates and avoids coupling email delivery to
    # an HTTP request or test-client template instrumentation.
    return Engine.get_default().get_template(name).render(EmailTemplateContext(context))


def send_confirmation_email(subscription):
    site_url = settings.NEWSLETTER_SITE_URL
    confirm_url = f"{site_url}/newsletter/confirm?token={subscription.confirm_token}"
    context = {"subscription": subscription, "confirm_url": confirm_url}
    message = EmailMultiAlternatives(
        subject="Confirm your WorldStories weekly digest",
        body=render_email_template("newsletter/confirm.txt", context),
        from_email=settings.NEWSLETTER_FROM_EMAIL or settings.DEFAULT_FROM_EMAIL,
        to=[subscription.email],
        reply_to=(
            [settings.NEWSLETTER_REPLY_TO] if settings.NEWSLETTER_REPLY_TO else None
        ),
    )
    message.attach_alternative(
        render_email_template("newsletter/confirm.html", context), "text/html"
    )
    return send_message_or_raise(message)


def digest_url(path, issue_date, **query):
    base = f"{settings.NEWSLETTER_SITE_URL}/{path.lstrip('/')}"
    parts = urlsplit(base)
    params = dict(parse_qsl(parts.query))
    params.update(query)
    params.update(
        {
            "utm_source": "digest",
            "utm_medium": "email",
            "utm_campaign": f"digest-{issue_date}",
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))


def prepare_digest_context(data, issue_date, subscription=None):
    context = {**data, "issue_date": issue_date}
    context["new_stories"] = [
        {**item, "url": digest_url(f"/story/{item['slug']}", issue_date)}
        for item in data["new_stories"]
    ]
    context["new_posts"] = [
        {**item, "url": digest_url(f"/blog/{item['slug']}", issue_date)}
        for item in data["new_posts"]
    ]
    if data["story_of_the_week"]:
        item = data["story_of_the_week"]
        context["story_of_the_week"] = {
            **item,
            "url": digest_url(f"/story/{item['slug']}", issue_date),
        }
    if data["continue_reading"]:
        item = data["continue_reading"]
        context["continue_reading"] = {
            **item,
            "url": digest_url(item["reader_path"], issue_date),
        }
    token = subscription.unsubscribe_token if subscription else "test-copy"
    context.update(
        {
            "greeting_name": (
                subscription.user.display_name or subscription.user.username
                if subscription and subscription.user_id
                else "story lover"
            ),
            "unsubscribe_url": digest_url(
                "/newsletter/unsubscribe", issue_date, token=token
            ),
            "subscription_token": token,
            "manage_url": digest_url("/profile/settings", issue_date),
            "postal_address": settings.NEWSLETTER_POSTAL_ADDRESS,
        }
    )
    return context


def build_digest_message(subject, data, issue_date, recipient, subscription=None):
    context = prepare_digest_context(data, issue_date, subscription)
    message = EmailMultiAlternatives(
        subject=subject,
        body=render_email_template("newsletter/digest.txt", context),
        from_email=settings.NEWSLETTER_FROM_EMAIL,
        to=[recipient],
        reply_to=(
            [settings.NEWSLETTER_REPLY_TO] if settings.NEWSLETTER_REPLY_TO else None
        ),
    )
    message.attach_alternative(
        render_email_template("newsletter/digest.html", context), "text/html"
    )
    api_base = settings.NEWSLETTER_API_URL.rstrip("/")
    api_prefix = api_base if api_base.endswith("/api") else f"{api_base}/api"
    one_click_url = (
        f"{api_prefix}/newsletter/unsubscribe/{context['subscription_token']}/"
    )
    list_unsubscribe = [f"<{one_click_url}>"]
    if settings.NEWSLETTER_REPLY_TO:
        list_unsubscribe.append(f"<mailto:{settings.NEWSLETTER_REPLY_TO}>")
    message.extra_headers = {
        "List-Unsubscribe": ", ".join(list_unsubscribe),
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return message


def send_message_or_raise(message):
    sent_count = message.send()
    if sent_count != 1:
        raise RuntimeError(f"Email backend reported {sent_count} messages submitted")
    return getattr(message, "anymail_status", None)


def build_digest_subject(data: dict) -> str:
    return (
        f"New this week: {data['new_stories'][0]['title']}"
        if data["new_stories"]
        else "Your week in stories"
    )


def send_test_digest(recipient: str, story_slug: str | None = None, now: datetime | None = None):
    """Build and send a single test digest without creating DigestIssue/DigestDelivery rows."""
    from .digest import build_digest

    if not settings.NEWSLETTER_FROM_EMAIL:
        raise RuntimeError("NEWSLETTER_FROM_EMAIL is required")
    now = now or timezone.now()
    tz = ZoneInfo(settings.NEWSLETTER_SEND_TIMEZONE)
    issue_date = now.astimezone(tz).date()
    since = now - timedelta(days=7)
    if not story_slug:
        week_story = get_week_featured_story(issue_date)
        if week_story:
            story_slug = week_story.slug
    subscription = (
        NewsletterSubscription.objects.select_related("user")
        .filter(email__iexact=recipient)
        .first()
    )
    data = build_digest(since, subscription=subscription, story_slug=story_slug, now=now)
    subject = build_digest_subject(data)
    message = build_digest_message(subject, data, issue_date, recipient, subscription)
    return send_message_or_raise(message)
