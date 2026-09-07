"""Print the exact OAuth values each provider console needs.

The redirect URIs are *derived* from ``SOCIAL_ANALYTICS_PUBLIC_BASE_URL`` (or
overridden per provider), and every provider rejects a callback whose URI is
not a character-for-character match with what is registered. Rather than
reassembling those strings by hand, print what this deployment will actually
send.

    python manage.py show_oauth_config

Secrets are never printed — only whether they are set.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.social_media_analytics.integrations import meta, tiktok, youtube


def _mask(value: str) -> str:
    if not value:
        return "NOT SET"
    return f"set ({len(value)} chars, ends …{value[-4:]})"


class Command(BaseCommand):
    help = "Show the redirect URIs and scopes to register in each provider console."

    def handle(self, *args, **options):
        base = getattr(settings, "SOCIAL_ANALYTICS_PUBLIC_BASE_URL", "")
        ok = self.style.SUCCESS
        warn = self.style.WARNING

        self.stdout.write("")
        self.stdout.write(ok("Public base URL"))
        self.stdout.write(f"  {base or warn('NOT SET')}")
        if base.startswith("http://"):
            self.stdout.write(
                warn("  ! Providers require https for a live redirect URI.")
            )
        self.stdout.write("")

        providers = [
            (
                "META  (Facebook Page + Instagram)",
                "https://developers.facebook.com/apps/",
                getattr(settings, "META_REDIRECT_URI", ""),
                meta.SCOPES,
                [
                    ("META_APP_ID", getattr(settings, "META_APP_ID", "")),
                    ("META_APP_SECRET", getattr(settings, "META_APP_SECRET", "")),
                ],
                f"Graph API {getattr(settings, 'META_GRAPH_API_VERSION', '')}",
            ),
            (
                "GOOGLE  (YouTube)",
                "https://console.cloud.google.com/apis/credentials",
                getattr(settings, "YOUTUBE_REDIRECT_URI", ""),
                youtube.SCOPES,
                [
                    ("YOUTUBE_CLIENT_ID", getattr(settings, "YOUTUBE_CLIENT_ID", "")),
                    ("YOUTUBE_CLIENT_SECRET", getattr(settings, "YOUTUBE_CLIENT_SECRET", "")),
                ],
                "Enable: YouTube Data API v3 + YouTube Analytics API",
            ),
            (
                "TIKTOK",
                "https://developers.tiktok.com/apps/",
                getattr(settings, "TIKTOK_REDIRECT_URI", ""),
                tiktok.SCOPES,
                [
                    ("TIKTOK_CLIENT_KEY", getattr(settings, "TIKTOK_CLIENT_KEY", "")),
                    ("TIKTOK_CLIENT_SECRET", getattr(settings, "TIKTOK_CLIENT_SECRET", "")),
                ],
                "Products: Login Kit + Display API",
            ),
        ]

        for name, console, redirect, scopes, creds, note in providers:
            self.stdout.write(ok(name))
            self.stdout.write(f"  console      {console}")
            self.stdout.write(f"  note         {note}")
            self.stdout.write("  redirect URI (paste this exactly, trailing slash included):")
            self.stdout.write(f"    {redirect or warn('NOT SET')}")
            self.stdout.write(f"  scopes       {', '.join(scopes)}")
            for key, value in creds:
                marker = "" if value else "  <-- required to connect this platform"
                self.stdout.write(f"  {key:<24} {_mask(value)}{marker}")
            self.stdout.write("")
