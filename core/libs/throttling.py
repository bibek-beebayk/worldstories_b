import hmac

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle


class TrustedInternalOrAnonRateThrottle(AnonRateThrottle):
    """AnonRateThrottle, except requests carrying the correct
    X-Internal-SSR-Key header skip throttling entirely. The frontend's React
    Router loaders run server-side on the Node host and fetch this API
    directly — those calls never carry the actual site visitor's IP, so
    without this every visitor's page load would count against the same
    IP-keyed anon bucket and starve real traffic (see SSR_INTERNAL_API_KEY in
    settings). Real browser-originated requests never carry this header —
    the key is a server-only env var, never bundled into frontend JS."""

    def allow_request(self, request, view):
        key = request.META.get("HTTP_X_INTERNAL_SSR_KEY", "")
        if settings.SSR_INTERNAL_API_KEY and hmac.compare_digest(key, settings.SSR_INTERNAL_API_KEY):
            return True
        return super().allow_request(request, view)
