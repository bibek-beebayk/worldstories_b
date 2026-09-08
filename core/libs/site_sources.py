"""Companion-site routing guards for legacy main-site analytics writers."""
import os
from urllib.parse import urlsplit


def is_nepalikatha_request(request):
    origins = {"https://nepalikatha.worldstories.net"}
    configured = os.environ.get("NP_SITE_URL", "").rstrip("/")
    if configured:
        origins.add(configured)
    for value in (request.headers.get("Origin", ""), request.headers.get("Referer", "")):
        try:
            url = urlsplit(value)
            if f"{url.scheme}://{url.netloc}" in origins:
                return True
        except ValueError:
            pass
    return getattr(request, "query_params", request.GET).get("show_in_nepali_site", "").lower() == "true"
