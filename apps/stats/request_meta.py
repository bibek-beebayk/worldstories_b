"""Coarse, privacy-preserving request provenance for AnalyticsEvent.

Only the derived values ever reach the database: a country code, a keyed IP
hash, and a coarse browser bucket. The raw IP and the full User-Agent string
are deliberately never stored.
"""

import hashlib
import hmac
import re

from django.conf import settings

# Cloudflare uses XX for "no country data" and T1 for Tor exits; neither is a
# country, so both are treated as unknown.
_UNKNOWN_COUNTRIES = {"XX", "T1"}

_MOBILE_RE = re.compile(r"Mobi|Android|iPhone|iPad|iPod", re.I)

# Checked in order: in-app browsers and Edge/Samsung/Firefox all also carry a
# "Chrome" or "Safari" token, so the more specific families must win first.
_UA_FAMILIES = (
    ("facebook_app", re.compile(r"FBAN|FBAV|FB_IAB|FBIOS", re.I)),
    ("instagram_app", re.compile(r"Instagram", re.I)),
    ("edge", re.compile(r"Edg(?:e|A|iOS)?/", re.I)),
    ("samsung", re.compile(r"SamsungBrowser", re.I)),
    ("firefox", re.compile(r"Firefox|FxiOS", re.I)),
    ("chrome", re.compile(r"Chrome|CriOS|Chromium", re.I)),
    ("safari", re.compile(r"Safari", re.I)),
)


def country_code_from_request(request):
    raw = (request.META.get("HTTP_CF_IPCOUNTRY") or "").strip().upper()
    if len(raw) != 2 or not raw.isascii() or not raw.isalpha() or raw in _UNKNOWN_COUNTRIES:
        return ""
    return raw


def hash_ip(ip_address):
    if not ip_address:
        return ""
    key = getattr(settings, "ANALYTICS_IP_HASH_KEY", "") or settings.SECRET_KEY
    return hmac.new(key.encode(), ip_address.encode(), hashlib.sha256).hexdigest()


def ua_family(user_agent):
    user_agent = (user_agent or "").strip()
    if not user_agent:
        return ""
    family = "other"
    for name, pattern in _UA_FAMILIES:
        if pattern.search(user_agent):
            family = name
            break
    return f"{family}_{'mobile' if _MOBILE_RE.search(user_agent) else 'desktop'}"


def request_provenance(request):
    # Imported here: apps.story.api imports from apps.stats at module load.
    from apps.story.api import get_client_ip

    return {
        "country_code": country_code_from_request(request),
        "ip_hash": hash_ip(get_client_ip(request)),
        "ua_family": ua_family(request.META.get("HTTP_USER_AGENT", "")),
    }
