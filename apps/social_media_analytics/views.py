"""Non-API views: the OAuth redirect endpoints the providers call back to.

These are plain Django views, not DRF ones, because the caller is a browser
following a 302 from Meta/Google/TikTok — there is no bearer token on the
request and the response is HTML, not JSON. The CSRF ``state`` created in
``services.build_authorize_url`` is what ties the callback back to a user,
so no session is required either.

The Flutter app opens the provider URL in a browser/webview and simply waits;
when ``SOCIAL_ANALYTICS_APP_REDIRECT`` is configured the callback bounces to
that deep link so the app can close the webview itself, otherwise the user
sees a short confirmation page and closes it manually.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from . import services
from .integrations.base import IntegrationError

logger = logging.getLogger(__name__)


_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; display: grid;
         place-items: center; min-height: 100vh; background: #0f1115; color: #e8eaed; }}
  .card {{ max-width: 30rem; padding: 2rem; text-align: center; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
  p {{ margin: 0; color: #9aa0a6; }}
  .ok {{ color: #34a853; }} .bad {{ color: #ea4335; }}
</style>
<div class="card">
  <h1 class="{cls}">{title}</h1>
  <p>{body}</p>
</div>
"""


def _render(title: str, body: str, *, ok: bool, status: int = 200) -> HttpResponse:
    return HttpResponse(
        _PAGE.format(
            title=escape(title), body=escape(body), cls="ok" if ok else "bad"
        ),
        status=status,
    )


@require_GET
@csrf_exempt
def oauth_callback(request, provider: str):
    """Shared redirect handler for all three OAuth providers.

    Providers are given per-provider redirect URIs (their consoles require an
    exact match), but the handling is identical, so they share one view keyed
    on the ``provider`` path segment.
    """

    error = request.GET.get("error") or request.GET.get("error_description")
    if error:
        # Provider-side denial (user hit "Cancel", app not approved, …).
        logger.info("OAuth callback for %s returned an error", provider)
        return _render(
            "Connection cancelled",
            f"{provider.title()} did not grant access: {error}",
            ok=False,
            status=400,
        )

    code = request.GET.get("code", "")
    state = request.GET.get("state", "")
    if not (code and state):
        return _render(
            "Invalid callback",
            "The provider did not send an authorisation code.",
            ok=False,
            status=400,
        )

    try:
        connections = services.complete_oauth(state, code)
    except IntegrationError as exc:
        logger.warning("OAuth completion failed for %s: %s", provider, exc)
        return _render("Connection failed", str(exc), ok=False, status=400)
    except Exception:  # noqa: BLE001 - never leak a stack trace to a browser
        logger.exception("Unexpected OAuth failure for %s", provider)
        return _render(
            "Connection failed",
            "Something went wrong completing the connection. Check the server logs.",
            ok=False,
            status=500,
        )

    app_redirect = getattr(settings, "SOCIAL_ANALYTICS_APP_REDIRECT", "")
    if app_redirect:
        joiner = "&" if "?" in app_redirect else "?"
        return HttpResponseRedirect(f"{app_redirect}{joiner}status=connected&provider={provider}")

    names = ", ".join(
        f"{c.get_platform_display()} ({c.display_name or c.external_account_id})"
        for c in connections
    )
    return _render(
        "Connected",
        f"Linked: {names}. You can close this window and return to the app.",
        ok=True,
    )
