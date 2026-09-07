"""YouTube Data API v3 + YouTube Analytics API v2 integration.

Uses the Google client libraries named in the spec: ``google-auth-oauthlib``
for the authorisation-code flow, ``google-auth`` for refresh, and
``google-api-python-client`` for the data calls.

Quota discipline (10,000 units/day, spec 3.4)
---------------------------------------------
Cost per call, from Google's published quota table:

* ``search.list`` — **100 units**. Never used here. It is the obvious way to
  list a channel's videos and it is also the single fastest way to burn the
  daily quota; one 50-video page costs 100 units and gives no statistics.
* ``channels.list`` — 1 unit. Gives the channel's ``uploads`` playlist id and
  the account-level statistics in the same call.
* ``playlistItems.list`` — 1 unit per page of up to 50. Walking the uploads
  playlist is the cheap equivalent of ``search.list``.
* ``videos.list`` — 1 unit per call regardless of how many ids are batched,
  so video ids are chunked 50 at a time.

A full sync of a 500-video channel therefore costs roughly
1 + 10 + 10 = 21 units, leaving the daily quota essentially untouched.

The Analytics API (``yt-analytics.readonly``) has a separate quota and is
used only for the channel-level daily views series, because the Data API
reports lifetime totals only. Per-video time series come from our own
repeated ``MetricSnapshot`` polling, not from Google.
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

from ..models import ContentType
from .base import (
    AccountMetrics,
    FetchedContent,
    FetchedMetrics,
    IntegrationError,
    OAuthTokens,
    TokenExpiredError,
    to_int,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

PLAYLIST_PAGE_SIZE = 50
VIDEO_ID_BATCH = 50
MAX_PLAYLIST_PAGES = 20  # 1,000 most recent uploads is plenty for a personal channel


def _client_config() -> dict:
    """The ``installed``-style config google-auth-oauthlib expects.

    The project already has ``GOOGLE_CLIENT_ID``/``GOOGLE_CLIENT_SECRET`` for
    site sign-in; those are a *different* OAuth client (different consent
    screen scopes and redirect URI), so this feature has its own
    ``YOUTUBE_CLIENT_ID``/``YOUTUBE_CLIENT_SECRET`` and falls back to the
    project-wide pair only if they are unset.
    """

    client_id = getattr(settings, "YOUTUBE_CLIENT_ID", "") or getattr(
        settings, "GOOGLE_CLIENT_ID", ""
    )
    client_secret = getattr(settings, "YOUTUBE_CLIENT_SECRET", "") or getattr(
        settings, "GOOGLE_CLIENT_SECRET", ""
    )
    redirect_uri = getattr(settings, "YOUTUBE_REDIRECT_URI", "")
    if not (client_id and client_secret and redirect_uri):
        raise IntegrationError(
            "YouTube OAuth is not configured (YOUTUBE_CLIENT_ID / "
            "YOUTUBE_CLIENT_SECRET / YOUTUBE_REDIRECT_URI)."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def _flow(state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    config = _client_config()
    flow = Flow.from_client_config(config, scopes=SCOPES, state=state)
    flow.redirect_uri = config["web"]["redirect_uris"][0]
    return flow


def _credentials(connection):
    """Build ``Credentials`` from a stored connection, refreshing if stale."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    config = _client_config()["web"]
    creds = Credentials(
        token=connection.access_token or None,
        refresh_token=connection.refresh_token or None,
        token_uri=TOKEN_URI,
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=SCOPES,
    )
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # google-auth raises RefreshError subclasses
            raise TokenExpiredError(f"YouTube token refresh failed: {exc.__class__.__name__}") from exc
        # Persist the rotated access token so the next run starts valid.
        connection.access_token = creds.token
        if creds.expiry:
            connection.token_expires_at = (
                timezone.make_aware(creds.expiry, dt_timezone.utc)
                if timezone.is_naive(creds.expiry)
                else creds.expiry
            )
        connection.save(update_fields=["access_token", "token_expires_at"])
    return creds


def _service(connection, name: str, version: str):
    from googleapiclient.discovery import build

    return build(
        name,
        version,
        credentials=_credentials(connection),
        cache_discovery=False,
    )


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def authorize_url(state: str) -> str:
    """Google consent URL.

    ``access_type=offline`` + ``prompt=consent`` is required to receive a
    refresh token; Google only issues one on the *first* grant otherwise, so
    a reconnect after a disconnect would silently come back refresh-less.
    """

    flow = _flow(state=state)
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return url


def exchange_code(code: str, state: str = "") -> list[OAuthTokens]:
    flow = _flow(state=state or None)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise IntegrationError(f"YouTube code exchange failed: {exc.__class__.__name__}") from exc

    creds = flow.credentials
    expires_at = None
    if creds.expiry:
        expires_at = (
            timezone.make_aware(creds.expiry, dt_timezone.utc)
            if timezone.is_naive(creds.expiry)
            else creds.expiry
        )

    channel_id, title = _identify_channel(creds)
    return [
        OAuthTokens(
            access_token=creds.token or "",
            refresh_token=creds.refresh_token or "",
            expires_at=expires_at,
            external_account_id=channel_id,
            display_name=title,
            extra={"uploads_playlist_id": ""},
        )
    ]


def _identify_channel(creds) -> tuple[str, str]:
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    response = youtube.channels().list(part="snippet", mine=True).execute()
    items = response.get("items") or []
    if not items:
        raise IntegrationError("The Google account has no YouTube channel.")
    item = items[0]
    return str(item.get("id", "")), (item.get("snippet") or {}).get("title", "")


def refresh(connection) -> OAuthTokens:
    """Force a token refresh; ``_credentials`` already persists the result."""
    creds = _credentials(connection)
    expires_at = None
    if creds.expiry:
        expires_at = (
            timezone.make_aware(creds.expiry, dt_timezone.utc)
            if timezone.is_naive(creds.expiry)
            else creds.expiry
        )
    return OAuthTokens(
        access_token=creds.token or "",
        refresh_token=creds.refresh_token or connection.refresh_token,
        expires_at=expires_at,
        external_account_id=connection.external_account_id,
        display_name=connection.display_name,
        extra=connection.extra or {},
    )


# --------------------------------------------------------------------------
# Data pulls
# --------------------------------------------------------------------------

def fetch_account(connection) -> AccountMetrics:
    """Channel statistics — 1 quota unit."""
    youtube = _service(connection, "youtube", "v3")
    response = (
        youtube.channels()
        .list(part="snippet,statistics,contentDetails", id=connection.external_account_id)
        .execute()
    )
    items = response.get("items") or []
    if not items:
        raise IntegrationError("Connected YouTube channel is no longer visible.")
    item = items[0]
    stats = item.get("statistics") or {}

    # Cache the uploads playlist id so fetch_content doesn't re-request it.
    uploads = (
        ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        or ""
    )
    extra = dict(connection.extra or {})
    if uploads and extra.get("uploads_playlist_id") != uploads:
        extra["uploads_playlist_id"] = uploads
        connection.extra = extra
        connection.save(update_fields=["extra"])

    return AccountMetrics(
        # hiddenSubscriberCount channels omit subscriberCount entirely.
        follower_count=to_int(stats.get("subscriberCount")),
        total_views_lifetime=to_int(stats.get("viewCount")),
        display_name=(item.get("snippet") or {}).get("title", ""),
    )


def fetch_content(connection) -> list[FetchedContent]:
    """Uploads playlist walk + batched ``videos.list`` statistics.

    Total cost: ~1 unit per 50 videos listed, plus 1 unit per 50 videos'
    statistics. ``search.list`` is deliberately not used — see module
    docstring.
    """

    youtube = _service(connection, "youtube", "v3")
    uploads = (connection.extra or {}).get("uploads_playlist_id")
    if not uploads:
        fetch_account(connection)  # populates uploads_playlist_id
        uploads = (connection.extra or {}).get("uploads_playlist_id")
    if not uploads:
        raise IntegrationError("Could not resolve the channel's uploads playlist.")

    video_ids: list[str] = []
    page_token = None
    for _ in range(MAX_PLAYLIST_PAGES):
        response = (
            youtube.playlistItems()
            .list(
                part="contentDetails",
                playlistId=uploads,
                maxResults=PLAYLIST_PAGE_SIZE,
                pageToken=page_token,
            )
            .execute()
        )
        for row in response.get("items") or []:
            vid = ((row.get("contentDetails") or {}).get("videoId")) or ""
            if vid:
                video_ids.append(vid)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    items: list[FetchedContent] = []
    for start in range(0, len(video_ids), VIDEO_ID_BATCH):
        batch = video_ids[start : start + VIDEO_ID_BATCH]
        response = (
            youtube.videos()
            .list(part="snippet,statistics,contentDetails", id=",".join(batch))
            .execute()
        )
        for row in response.get("items") or []:
            snippet = row.get("snippet") or {}
            stats = row.get("statistics") or {}
            thumbs = snippet.get("thumbnails") or {}
            thumb = (
                thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}
            ).get("url", "")
            duration = (row.get("contentDetails") or {}).get("duration", "")
            items.append(
                FetchedContent(
                    platform_content_id=str(row.get("id", "")),
                    content_type=_classify(duration),
                    caption_or_title=snippet.get("title", "") or "",
                    permalink=f"https://www.youtube.com/watch?v={row.get('id', '')}",
                    thumbnail_url=thumb,
                    published_at=_parse_time(snippet.get("publishedAt")),
                    metrics=FetchedMetrics(
                        views=to_int(stats.get("viewCount")),
                        likes=to_int(stats.get("likeCount")),
                        comments=to_int(stats.get("commentCount")),
                        # YouTube's public API exposes none of these: shares
                        # and saves live in Analytics only as aggregates, and
                        # there is no per-video impressions/reach in the Data
                        # API at all.
                        shares=None,
                        saves=None,
                        impressions=None,
                        reach=None,
                    ),
                )
            )
    return items


def _classify(iso_duration: str) -> str:
    """Treat uploads of 60s or less as Shorts (mapped to the reel type).

    This is a heuristic: the Data API has no "is a Short" flag, and duration
    is the signal YouTube's own tooling effectively uses.
    """

    if not iso_duration.startswith("PT"):
        return ContentType.VIDEO
    body = iso_duration[2:]
    if "H" in body or "M" in body:
        # PT1M0S is exactly 60s, still a Short; anything with minutes > 1 is not.
        if body.startswith("1M") and body in ("1M", "1M0S"):
            return ContentType.REEL
        return ContentType.VIDEO
    seconds = to_int(body.rstrip("S")) or 0
    return ContentType.REEL if seconds <= 60 else ContentType.VIDEO


def _parse_time(value: str | None):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_account_daily_views(connection, days: int = 30) -> list[tuple[str, int]]:
    """Channel daily views from the Analytics API.

    Optional enrichment — the dashboard works without it, since account
    trends are also reconstructable from our own ``AccountSnapshot`` rows.
    Returns ``[(YYYY-MM-DD, views), ...]``.
    """

    analytics = _service(connection, "youtubeAnalytics", "v2")
    end = timezone.now().date()
    start = end - timedelta(days=days)
    response = (
        analytics.reports()
        .query(
            ids=f"channel=={connection.external_account_id}",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="views",
            dimensions="day",
            sort="day",
        )
        .execute()
    )
    return [(row[0], to_int(row[1]) or 0) for row in (response.get("rows") or [])]
