"""Best-effort IP -> country/city resolution for login analytics.

Uses ip-api.com's free tier (no API key, ~45 req/min per source IP — our
server's outbound IP, shared across every login). Swap resolve_ip_location's
body for a MaxMind GeoLite2 (geoip2) lookup or another provider if that rate
limit ever becomes a problem — record_login and everything downstream of it
(the admin analytics endpoints, the heatmap) only depend on the
(country, country_code, city) tuple this returns, not on how it's produced.

The lookup runs in a background thread so a slow/unreachable geolocation
provider can never add latency to — or block — an actual login.
"""

import logging
import threading

import requests
from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)

GEOLOCATION_TIMEOUT_SECONDS = 2
# IPs don't change geo minute-to-minute, and this keeps a chatty user
# (or shared office/NAT IP logging in repeatedly) from re-querying the
# provider's rate-limited free tier for the same address every time.
GEOLOCATION_CACHE_SECONDS = 60 * 60 * 24


def get_client_ip(request):
    """Trusts X-Forwarded-For's first hop only if it's present — set by
    any reverse proxy/load balancer in front of the app (nginx, Render,
    Railway, Fly all commonly add it) — falling back to REMOTE_ADDR for a
    direct connection. Not hardened against a client spoofing this header
    directly: fine here since it only ever feeds a best-effort analytics
    lookup, never an access-control decision."""
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def resolve_ip_location(ip_address):
    """Returns (country, country_code, city) for an IP, or ("", "", "") if
    it can't be resolved (local/private address, provider error, timeout,
    etc). Never raises — geolocation is a nice-to-have that must not be
    able to break whatever called it."""
    if not ip_address or ip_address in ("127.0.0.1", "::1"):
        return "", "", ""

    cache_key = f"geoip:{ip_address}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result = ("", "", "")
    try:
        response = requests.get(
            f"http://ip-api.com/json/{ip_address}",
            params={"fields": "status,country,countryCode,city"},
            timeout=GEOLOCATION_TIMEOUT_SECONDS,
        )
        data = response.json()
        if data.get("status") == "success":
            result = (data.get("country") or "", data.get("countryCode") or "", data.get("city") or "")
    except (requests.RequestException, ValueError):
        logger.warning("IP geolocation lookup failed for %s", ip_address, exc_info=True)

    cache.set(cache_key, result, GEOLOCATION_CACHE_SECONDS)
    return result


def _record_login_location_async(user_id, ip_address):
    # Ad-hoc threads don't inherit the request-response cycle's connection
    # lifecycle, so this both starts clean and hands back a closeable
    # connection when it's done rather than leaking one per login.
    close_old_connections()
    try:
        from .models import UserLoginLocation

        country, country_code, city = resolve_ip_location(ip_address)
        UserLoginLocation.objects.create(
            user_id=user_id,
            ip_address=ip_address,
            country=country,
            country_code=country_code,
            city=city,
        )
    except Exception:
        logger.exception("Failed to record login location for user %s", user_id)
    finally:
        close_old_connections()


def record_login(request, user):
    """Call from every real login path (Google login, admin login) right
    after tokens are issued. Bumps login_count/last_login synchronously —
    nothing in this codebase did before, which is why the admin analytics
    "active users" and "login frequency" numbers were always empty — and
    fires off the geo lookup in the background so it can't slow down the
    login response."""
    user.login_count = (user.login_count or 0) + 1
    user.last_login = timezone.now()
    user.save(update_fields=["login_count", "last_login"])

    ip_address = get_client_ip(request)
    threading.Thread(
        target=_record_login_location_async,
        args=(user.pk, ip_address),
        daemon=True,
    ).start()
