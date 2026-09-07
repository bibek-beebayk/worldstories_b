"""TikTok Login Kit (OAuth 2.0) + Display API integration.

Access tier and what it means for this app
------------------------------------------
The Display API tier this integration targets is read-only access to the
*authenticated user's own* videos. Concretely:

* There is **no historical-trend endpoint**. ``/v2/video/list/`` returns the
  video's *current* cumulative counters (view/like/comment/share) and nothing
  time-bound. There is no "views on day X" query at any Display API tier.
* There is **no follower-demographics endpoint** — no age/gender/country
  breakdown, no follower-growth series. ``/v2/user/info/`` returns a single
  current ``follower_count``.
* Therefore **all** of this app's TikTok history comes from our own repeated
  polling: each Celery run writes a new ``MetricSnapshot`` / ``AccountSnapshot``
  row, and the trend charts are built entirely from those rows. If the
  scheduler is down for a week, that week is simply absent — TikTok cannot
  backfill it. This is the one platform where sync cadence directly
  determines data quality.

OAuth notes
-----------
* TikTok uses ``client_key`` (not ``client_id``) as the parameter name.
* Access tokens live 24 hours; refresh tokens live 365 days and are
  **rotated** on every refresh, so the new refresh token must be persisted or
  the connection dies a day later.
* PKCE is required for web/unaudited clients; the ``code_verifier`` is stored
  on the ``OAuthState`` row and replayed at exchange time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone

from ..models import ContentType
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

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"

SCOPES = ["user.info.basic", "user.info.stats", "video.list"]

VIDEO_PAGE_SIZE = 20  # Display API caps max_count at 20
MAX_PAGES = 10

USER_FIELDS = ["open_id", "display_name", "follower_count", "likes_count", "video_count"]
VIDEO_FIELDS = [
    "id",
    "title",
    "video_description",
    "cover_image_url",
    "share_url",
    "create_time",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
]


def _config() -> tuple[str, str, str]:
    client_key = getattr(settings, "TIKTOK_CLIENT_KEY", "")
    client_secret = getattr(settings, "TIKTOK_CLIENT_SECRET", "")
    redirect_uri = getattr(settings, "TIKTOK_REDIRECT_URI", "")
    if not (client_key and client_secret and redirect_uri):
        raise IntegrationError(
            "TikTok OAuth is not configured (TIKTOK_CLIENT_KEY / "
            "TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI)."
        )
    return client_key, client_secret, redirect_uri


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def authorize_url(state: str, code_challenge: str = "") -> str:
    client_key, _, redirect_uri = _config()
    params = {
        # NB: client_key, not client_id — TikTok is the odd one out.
        "client_key": client_key,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, code_verifier: str = "") -> list[OAuthTokens]:
    client_key, client_secret, redirect_uri = _config()
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier

    data = request_json(
        "POST",
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if data.get("error"):
        raise IntegrationError(f"TikTok token exchange failed: {data.get('error')}")

    access_token = data.get("access_token", "")
    if not access_token:
        raise IntegrationError("TikTok token exchange returned no access_token")

    expires_at = timezone.now() + timedelta(seconds=to_int(data.get("expires_in")) or 86400)
    open_id = str(data.get("open_id", ""))

    display_name = ""
    try:
        info = _user_info(access_token)
        display_name = info.get("display_name", "")
        open_id = open_id or str(info.get("open_id", ""))
    except IntegrationError:
        # Not fatal — the connection is usable, the label just stays blank
        # until the first sync fills it in.
        logger.warning("TikTok connect succeeded but user info lookup failed")

    return [
        OAuthTokens(
            access_token=access_token,
            refresh_token=data.get("refresh_token", ""),
            expires_at=expires_at,
            external_account_id=open_id,
            display_name=display_name,
            extra={"scope": data.get("scope", "")},
        )
    ]


def refresh(connection) -> OAuthTokens:
    """Exchange the rotating refresh token for a new 24h access token.

    TikTok rotates the refresh token on every call, so the caller **must**
    persist ``refresh_token`` from the result, not keep the old one.
    """

    client_key, client_secret, _ = _config()
    if not connection.refresh_token:
        raise IntegrationError("TikTok connection has no refresh token")

    data = request_json(
        "POST",
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": connection.refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if data.get("error") or not data.get("access_token"):
        raise IntegrationError(f"TikTok refresh failed: {data.get('error', 'no token')}")

    return OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", "") or connection.refresh_token,
        expires_at=timezone.now() + timedelta(seconds=to_int(data.get("expires_in")) or 86400),
        external_account_id=connection.external_account_id,
        display_name=connection.display_name,
        extra=connection.extra or {},
    )


# --------------------------------------------------------------------------
# Data pulls
# --------------------------------------------------------------------------

def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _user_info(access_token: str) -> dict:
    payload = request_json(
        "GET",
        USER_INFO_URL,
        params={"fields": ",".join(USER_FIELDS)},
        headers=_auth_headers(access_token),
    )
    return (payload.get("data") or {}).get("user") or {}


def fetch_account(connection) -> AccountMetrics:
    """Current follower count. No history is available — see module docstring."""
    user = _user_info(connection.access_token)
    return AccountMetrics(
        follower_count=to_int(user.get("follower_count")),
        # TikTok reports total *likes* received, not total views, so the
        # lifetime-views column stays null for this platform.
        total_views_lifetime=None,
        display_name=user.get("display_name", ""),
    )


def fetch_content(connection) -> list[FetchedContent]:
    """Walk ``/v2/video/list/`` and map current counters onto a snapshot.

    Every number here is a *cumulative lifetime* counter as of right now.
    Deltas between runs are what the trend charts actually plot.
    """

    items: list[FetchedContent] = []
    cursor = None
    for _ in range(MAX_PAGES):
        body: dict = {"max_count": VIDEO_PAGE_SIZE}
        if cursor:
            body["cursor"] = cursor
        payload = request_json(
            "POST",
            VIDEO_LIST_URL,
            params={"fields": ",".join(VIDEO_FIELDS)},
            # /v2/video/list/ takes a JSON body (unlike the form-encoded
            # OAuth token endpoint above).
            json_body=body,
            headers=_auth_headers(connection.access_token),
        )
        data = payload.get("data") or {}
        for row in data.get("videos") or []:
            created = row.get("create_time")
            published = (
                datetime.fromtimestamp(created, tz=dt_timezone.utc)
                if isinstance(created, (int, float))
                else None
            )
            items.append(
                FetchedContent(
                    platform_content_id=str(row.get("id", "")),
                    content_type=ContentType.VIDEO,
                    caption_or_title=row.get("title") or row.get("video_description") or "",
                    permalink=row.get("share_url", "") or "",
                    thumbnail_url=row.get("cover_image_url", "") or "",
                    published_at=published,
                    metrics=FetchedMetrics(
                        views=to_int(row.get("view_count")),
                        likes=to_int(row.get("like_count")),
                        comments=to_int(row.get("comment_count")),
                        shares=to_int(row.get("share_count")),
                        # Display API exposes neither of these at any tier.
                        saves=None,
                        impressions=None,
                        reach=None,
                    ),
                )
            )
        if not data.get("has_more"):
            break
        cursor = data.get("cursor")
        if not cursor:
            break
    return items
