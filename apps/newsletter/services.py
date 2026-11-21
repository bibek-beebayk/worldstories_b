from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import Context, Engine
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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
