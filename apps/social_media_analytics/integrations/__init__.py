"""Platform integration registry.

``tasks.py`` and the OAuth views resolve a platform string to a module here
rather than importing the platform modules directly, so adding a platform is
a one-line change and nothing else has to know the roster.

Facebook and Instagram both map to :mod:`.meta` — one Meta app, one OAuth
grant, two ``PlatformConnection`` rows.
"""

from __future__ import annotations

from types import ModuleType

from ..models import Platform
from . import meta, tiktok, youtube
from .base import (  # re-exported for convenience
    AccountMetrics,
    FetchedContent,
    FetchedMetrics,
    IntegrationError,
    OAuthTokens,
    TokenExpiredError,
)

REGISTRY: dict[str, ModuleType] = {
    Platform.FACEBOOK: meta,
    Platform.INSTAGRAM: meta,
    Platform.YOUTUBE: youtube,
    Platform.TIKTOK: tiktok,
}

# Platforms that share a single OAuth grant. The connect flow is keyed on the
# *provider*, not on the platform, so "connect Instagram" and "connect
# Facebook" are the same authorisation.
PROVIDER_FOR_PLATFORM = {
    Platform.FACEBOOK: "meta",
    Platform.INSTAGRAM: "meta",
    Platform.YOUTUBE: "youtube",
    Platform.TIKTOK: "tiktok",
}

PROVIDER_MODULES: dict[str, ModuleType] = {
    "meta": meta,
    "youtube": youtube,
    "tiktok": tiktok,
}


def get_integration(platform: str) -> ModuleType:
    try:
        return REGISTRY[platform]
    except KeyError as exc:
        raise IntegrationError(f"Unknown platform: {platform!r}") from exc


def get_provider_module(provider: str) -> ModuleType:
    try:
        return PROVIDER_MODULES[provider]
    except KeyError as exc:
        raise IntegrationError(f"Unknown OAuth provider: {provider!r}") from exc


__all__ = [
    "REGISTRY",
    "PROVIDER_FOR_PLATFORM",
    "PROVIDER_MODULES",
    "get_integration",
    "get_provider_module",
    "AccountMetrics",
    "FetchedContent",
    "FetchedMetrics",
    "IntegrationError",
    "OAuthTokens",
    "TokenExpiredError",
]
