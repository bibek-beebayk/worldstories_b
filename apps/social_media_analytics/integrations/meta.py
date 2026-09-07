"""Meta Graph API integration: Facebook Page + Instagram Business/Creator.

One OAuth app covers both. An Instagram Business or Creator account is only
reachable through the Facebook Page it is linked to, so the flow is always:

    user grants -> short-lived user token -> long-lived user token
    -> GET /me/accounts -> per-Page token (+ linked IG user id)

Two ``PlatformConnection`` rows come out of a single grant — one
``facebook``, one ``instagram`` — because the dashboard treats them as
separate platforms. Both carry the *Page* access token: IG Graph calls are
authorised by the Page token, not by a separate Instagram credential.

Platform quirks worth knowing
-----------------------------
* **Page tokens derived from a long-lived user token do not expire** as long
  as the user token stays valid, but the underlying long-lived *user* token
  lasts ~60 days. We store the user token's expiry on both rows and refresh
  proactively (see :func:`refresh`), because once the user token dies every
  Page token minted from it dies with it.
* ``fb_exchange_token`` on an already-long-lived token returns a *new*
  60-day token, so a periodic refresh keeps the connection alive
  indefinitely without user interaction.
* Meta **removed ``impressions`` for Instagram media created on/after
  2024-04-21** (Graph API v22+) and replaced it with ``views``. We ask for
  both and store whichever comes back; the other stays null.
* Instagram gives no ``shares`` on media insights for most media types, and
  Facebook Page posts give no ``saves`` — those columns stay null per the
  table in ``models.MetricSnapshot``.
* Story insights expire after 24h; we only pull FEED/REELS media, so stories
  are out of scope for v1 rather than silently producing gaps.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from django.conf import settings
from django.utils import timezone

from ..models import ContentType, Platform
from .base import (
    AccountMetrics,
    FetchedContent,
    FetchedMetrics,
    IntegrationError,
    OAuthTokens,
    request_json,
    to_int,
)

logger = logging.getLogger(__name__)

GRAPH_VERSION = getattr(settings, "META_GRAPH_API_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
DIALOG_BASE = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_manage_insights",
]

# Refresh a long-lived user token once it is inside this window of expiring.
REFRESH_WINDOW = timedelta(days=10)

MEDIA_PAGE_LIMIT = 50
MAX_PAGES = 10  # hard stop so a runaway cursor can't loop forever


def _parse_time(value: str | None) -> datetime | None:
    """Parse Meta's ISO-8601 timestamps (``2024-05-01T10:00:00+0000``)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Meta emits +0000 without a colon, which older parsers reject.
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None


def _config() -> tuple[str, str, str]:
    app_id = getattr(settings, "META_APP_ID", "")
    app_secret = getattr(settings, "META_APP_SECRET", "")
    redirect_uri = getattr(settings, "META_REDIRECT_URI", "")
    if not (app_id and app_secret and redirect_uri):
        raise IntegrationError(
            "Meta OAuth is not configured (META_APP_ID / META_APP_SECRET / "
            "META_REDIRECT_URI)."
        )
    return app_id, app_secret, redirect_uri


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def authorize_url(state: str) -> str:
    """Build the Facebook Login dialog URL the app opens in a browser."""
    from urllib.parse import urlencode

    app_id, _, redirect_uri = _config()
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(SCOPES),
            "response_type": "code",
        }
    )
    return f"{DIALOG_BASE}?{query}"


def exchange_code(code: str) -> list[OAuthTokens]:
    """Trade an auth code for per-Page credentials.

    Returns *one or two* token bundles — the Page itself, and the linked
    Instagram Business account when there is one. The caller writes one
    ``PlatformConnection`` per bundle.
    """

    app_id, app_secret, redirect_uri = _config()

    short = request_json(
        "GET",
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    short_token = short.get("access_token")
    if not short_token:
        raise IntegrationError("Meta code exchange returned no access_token")

    long_lived = request_json(
        "GET",
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
    )
    user_token = long_lived.get("access_token", "")
    # Meta returns seconds-to-live; ~60 days for a long-lived user token.
    expires_in = to_int(long_lived.get("expires_in")) or 60 * 24 * 3600
    expires_at = timezone.now() + timedelta(seconds=expires_in)

    return _bundles_from_user_token(user_token, expires_at)


def _bundles_from_user_token(user_token: str, expires_at: datetime) -> list[OAuthTokens]:
    accounts = request_json(
        "GET",
        f"{GRAPH_BASE}/me/accounts",
        params={
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "access_token": user_token,
        },
    )
    pages = accounts.get("data") or []
    if not pages:
        raise IntegrationError(
            "No Facebook Pages are available to this account — the analytics "
            "app needs a Page with an admin role."
        )

    bundles: list[OAuthTokens] = []
    for page in pages:
        page_token = page.get("access_token", "")
        bundles.append(
            OAuthTokens(
                access_token=page_token,
                # The *user* token is what actually expires; keep it so the
                # refresh task has something to exchange.
                refresh_token=user_token,
                expires_at=expires_at,
                external_account_id=str(page.get("id", "")),
                display_name=page.get("name", ""),
                extra={"platform": Platform.FACEBOOK, "page_id": str(page.get("id", ""))},
            )
        )

        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            bundles.append(
                OAuthTokens(
                    access_token=page_token,
                    refresh_token=user_token,
                    expires_at=expires_at,
                    external_account_id=str(ig["id"]),
                    display_name=ig.get("username", "") or page.get("name", ""),
                    extra={
                        "platform": Platform.INSTAGRAM,
                        "page_id": str(page.get("id", "")),
                        "ig_user_id": str(ig["id"]),
                    },
                )
            )
    return bundles


def refresh(connection) -> OAuthTokens:
    """Re-exchange the stored long-lived user token for a fresh 60-day one.

    Meta has no refresh-token grant; ``fb_exchange_token`` against a still-
    valid long-lived token is the supported way to extend. Once the token has
    actually expired there is no recovery — the user must reconnect.
    """

    app_id, app_secret, _ = _config()
    user_token = connection.refresh_token or connection.access_token
    if not user_token:
        raise IntegrationError("Meta connection has no stored token to refresh")

    payload = request_json(
        "GET",
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        },
    )
    new_user_token = payload.get("access_token", "")
    if not new_user_token:
        raise IntegrationError("Meta token refresh returned no access_token")
    expires_in = to_int(payload.get("expires_in")) or 60 * 24 * 3600
    expires_at = timezone.now() + timedelta(seconds=expires_in)

    # Page tokens are re-minted from the refreshed user token so the stored
    # per-Page credential also gets a new lease.
    page_id = (connection.extra or {}).get("page_id") or connection.external_account_id
    for bundle in _bundles_from_user_token(new_user_token, expires_at):
        if bundle.extra.get("page_id") == str(page_id):
            if bundle.extra.get("platform") == connection.platform:
                return bundle
    raise IntegrationError(
        "Refreshed Meta token no longer grants access to the connected Page"
    )


# --------------------------------------------------------------------------
# Data pulls
# --------------------------------------------------------------------------

def fetch_account(connection) -> AccountMetrics:
    """Follower counts. Facebook uses ``followers_count``, IG the same name."""

    if connection.platform == Platform.INSTAGRAM:
        ig_id = (connection.extra or {}).get("ig_user_id") or connection.external_account_id
        data = request_json(
            "GET",
            f"{GRAPH_BASE}/{ig_id}",
            params={
                "fields": "username,followers_count,media_count",
                "access_token": connection.access_token,
            },
        )
        return AccountMetrics(
            follower_count=to_int(data.get("followers_count")),
            # Instagram exposes no lifetime view total.
            total_views_lifetime=None,
            display_name=data.get("username", ""),
        )

    data = request_json(
        "GET",
        f"{GRAPH_BASE}/{connection.external_account_id}",
        params={
            # fan_count is the legacy "likes" number; followers_count is the
            # one that matches what the Page owner sees today.
            "fields": "name,followers_count,fan_count",
            "access_token": connection.access_token,
        },
    )
    return AccountMetrics(
        follower_count=to_int(data.get("followers_count")) or to_int(data.get("fan_count")),
        total_views_lifetime=None,
        display_name=data.get("name", ""),
    )


def fetch_content(connection) -> list[FetchedContent]:
    if connection.platform == Platform.INSTAGRAM:
        return _fetch_instagram_media(connection)
    return _fetch_page_posts(connection)


def _paged(url: str, params: dict) -> list[dict]:
    """Follow Graph cursor pagination up to ``MAX_PAGES``."""
    out: list[dict] = []
    payload = request_json("GET", url, params=params)
    for _ in range(MAX_PAGES):
        out.extend(payload.get("data") or [])
        nxt = ((payload.get("paging") or {}).get("next")) or ""
        if not nxt:
            break
        # ``next`` is a fully-formed URL with the token already embedded.
        payload = request_json("GET", nxt)
    return out


def _fetch_page_posts(connection) -> list[FetchedContent]:
    """Page feed + per-post insights.

    Reactions/comments/shares come from the edge summaries on the post object
    itself (cheap, one call), while reach and impressions need the separate
    ``/insights`` edge. We request insights inline via field expansion so the
    whole thing stays one round trip per page of results.
    """

    fields = ",".join(
        [
            "id",
            "message",
            "permalink_url",
            "created_time",
            "full_picture",
            "shares",
            "likes.summary(true).limit(0)",
            "comments.summary(true).limit(0)",
            "insights.metric(post_impressions,post_impressions_unique)",
        ]
    )
    rows = _paged(
        f"{GRAPH_BASE}/{connection.external_account_id}/posts",
        {
            "fields": fields,
            "limit": MEDIA_PAGE_LIMIT,
            "access_token": connection.access_token,
        },
    )

    items: list[FetchedContent] = []
    for row in rows:
        insights = {
            entry.get("name"): to_int(((entry.get("values") or [{}])[0]).get("value"))
            for entry in ((row.get("insights") or {}).get("data") or [])
        }
        impressions = insights.get("post_impressions")
        reach = insights.get("post_impressions_unique")
        items.append(
            FetchedContent(
                platform_content_id=str(row.get("id", "")),
                content_type=ContentType.POST,
                caption_or_title=row.get("message", "") or "",
                permalink=row.get("permalink_url", "") or "",
                thumbnail_url=row.get("full_picture", "") or "",
                published_at=_parse_time(row.get("created_time")),
                metrics=FetchedMetrics(
                    # Pages have no distinct "views" metric for feed posts;
                    # impressions is the closest and is stored on its own
                    # column, leaving views null rather than duplicating it.
                    views=None,
                    likes=to_int(((row.get("likes") or {}).get("summary") or {}).get("total_count")),
                    comments=to_int(
                        ((row.get("comments") or {}).get("summary") or {}).get("total_count")
                    ),
                    shares=to_int((row.get("shares") or {}).get("count")),
                    saves=None,  # not exposed for Page posts
                    impressions=impressions,
                    reach=reach,
                ),
            )
        )
    return items


def _fetch_instagram_media(connection) -> list[FetchedContent]:
    """IG media list + per-media insights.

    ``views`` supersedes ``impressions`` for media created on/after
    2024-04-21. Asking for a metric a given media object doesn't support
    makes Graph reject the *whole* insights call, so insights are fetched
    per media with a tolerant fallback rather than expanded inline.
    """

    ig_id = (connection.extra or {}).get("ig_user_id") or connection.external_account_id
    fields = ",".join(
        [
            "id",
            "caption",
            "media_type",
            "media_product_type",
            "media_url",
            "thumbnail_url",
            "permalink",
            "timestamp",
            "like_count",
            "comments_count",
        ]
    )
    rows = _paged(
        f"{GRAPH_BASE}/{ig_id}/media",
        {
            "fields": fields,
            "limit": MEDIA_PAGE_LIMIT,
            "access_token": connection.access_token,
        },
    )

    items: list[FetchedContent] = []
    for row in rows:
        media_id = str(row.get("id", ""))
        product = (row.get("media_product_type") or "FEED").upper()
        if product == "STORY":
            # Story insights vanish after 24h — excluded, see module docstring.
            continue

        metrics = FetchedMetrics(
            likes=to_int(row.get("like_count")),
            comments=to_int(row.get("comments_count")),
            shares=None,
            impressions=None,
            saves=None,
            reach=None,
            views=None,
        )
        insights = _instagram_media_insights(connection, media_id, product)
        metrics.views = insights.get("views")
        metrics.impressions = insights.get("impressions")
        metrics.reach = insights.get("reach")
        metrics.saves = insights.get("saved")
        metrics.shares = insights.get("shares")

        content_type = ContentType.REEL if product == "REELS" else ContentType.POST
        items.append(
            FetchedContent(
                platform_content_id=media_id,
                content_type=content_type,
                caption_or_title=row.get("caption", "") or "",
                permalink=row.get("permalink", "") or "",
                thumbnail_url=row.get("thumbnail_url") or row.get("media_url") or "",
                published_at=_parse_time(row.get("timestamp")),
                metrics=metrics,
            )
        )
    return items


def _instagram_media_insights(connection, media_id: str, product: str) -> dict[str, int | None]:
    """Best-effort insights for one media object.

    Metric support varies by media type and by creation date, and Graph fails
    the entire request if *any* requested metric is unsupported for that
    object. So we try the modern set first and fall back to the pre-v22 set,
    returning whatever we get rather than failing the whole sync over one
    post.
    """

    attempts = [
        ["reach", "saved", "shares", "views"],
        ["reach", "saved", "impressions"],
        ["reach"],
    ]
    for metrics in attempts:
        try:
            payload = request_json(
                "GET",
                f"{GRAPH_BASE}/{media_id}/insights",
                params={
                    "metric": ",".join(metrics),
                    "access_token": connection.access_token,
                },
            )
        except IntegrationError:
            continue
        return {
            entry.get("name"): to_int(((entry.get("values") or [{}])[0]).get("value"))
            for entry in (payload.get("data") or [])
        }
    logger.warning(
        "No Instagram insights available for media %s (%s)", media_id, product
    )
    return {}
