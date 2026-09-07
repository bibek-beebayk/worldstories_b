"""Shared interface and HTTP plumbing for the platform integrations.

Every platform module exposes the same three things so ``tasks.py`` and the
OAuth views can stay platform-agnostic:

* an ``*OAuthClient`` with ``authorize_url()`` / ``exchange_code()`` /
  ``refresh(connection)``
* a ``fetch_account(connection) -> AccountMetrics``
* a ``fetch_content(connection) -> list[FetchedContent]``

Outbound calls use ``requests``, which is what the rest of Worldstories
already uses (``requirements/base.txt``); ``httpx`` is installed as a
transitive dependency but is not the project's convention.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class IntegrationError(Exception):
    """Any failure talking to a platform. Carries no token material."""


class TokenExpiredError(IntegrationError):
    """The stored credential is no longer usable and needs a reconnect."""


@dataclass(slots=True)
class OAuthTokens:
    """Result of an authorisation-code exchange or a refresh."""

    access_token: str
    refresh_token: str = ""
    expires_at: datetime | None = None
    external_account_id: str = ""
    display_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AccountMetrics:
    follower_count: int | None = None
    total_views_lifetime: int | None = None
    display_name: str = ""


@dataclass(slots=True)
class FetchedMetrics:
    """Per-content metrics. ``None`` means "platform does not report it"."""

    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    impressions: int | None = None
    reach: int | None = None


@dataclass(slots=True)
class FetchedContent:
    platform_content_id: str
    content_type: str
    caption_or_title: str = ""
    permalink: str = ""
    thumbnail_url: str = ""
    published_at: datetime | None = None
    metrics: FetchedMetrics = field(default_factory=FetchedMetrics)


class PlatformIntegration(Protocol):
    """The contract each ``integrations/<platform>.py`` module satisfies."""

    platform: str

    def fetch_account(self, connection) -> AccountMetrics: ...

    def fetch_content(self, connection) -> list[FetchedContent]: ...


def request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Call a platform endpoint and return decoded JSON.

    Raises :class:`IntegrationError` with a message that is safe to log —
    query strings are stripped before anything reaches the logger, because
    Meta and TikTok both accept the access token as a query parameter and we
    must never log token material (spec 5).
    """

    safe_url = url.split("?", 1)[0]
    try:
        response = requests.request(
            method,
            url,
            params=params,
            data=data,
            json=json_body,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise IntegrationError(f"{method} {safe_url} failed: {exc.__class__.__name__}") from exc

    if response.status_code in (401, 403):
        raise TokenExpiredError(
            f"{method} {safe_url} returned {response.status_code} — credential rejected"
        )
    if response.status_code >= 400:
        # Bodies from these APIs carry error codes but not credentials.
        raise IntegrationError(
            f"{method} {safe_url} returned {response.status_code}: {response.text[:500]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise IntegrationError(f"{method} {safe_url} returned non-JSON body") from exc


def to_int(value: Any) -> int | None:
    """Coerce a platform-reported count to ``int``, or ``None`` if absent.

    Platforms are inconsistent: YouTube returns counts as strings, Meta
    returns ints, and both omit the key entirely rather than sending null
    when a metric is unavailable for that object.
    """

    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
