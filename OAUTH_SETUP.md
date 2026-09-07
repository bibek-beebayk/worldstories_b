# Connecting the four platforms — OAuth provider setup

One console per provider. Budget about 20 minutes each for Meta and Google,
and longer for TikTok, which gates the useful scopes behind a review.

**Get the exact values first.** Every provider rejects a callback whose
redirect URI is not a character-for-character match with what you registered,
so print what this deployment will actually send rather than assembling the
strings by hand:

```bash
python manage.py show_oauth_config
```

Run it where the production environment variables are set (Railway's shell, or
locally with `SOCIAL_ANALYTICS_PUBLIC_BASE_URL` pointed at the production host).
It prints each redirect URI, the scopes, and whether each credential is set —
never the secrets themselves.

---

## The thing worth knowing before you start

**You do not need App Review on any of these.** Every provider has a mode where
the app's own owner can use restricted scopes without review, and this is a
single-user personal tool, so that is the mode you want:

| Provider | Mode | Who can use it |
| --- | --- | --- |
| Meta | leave in **Development** | anyone with an app role (you) |
| Google | **In production**, unverified | you, past a warning screen |
| TikTok | **Sandbox** with a target user | the accounts you nominate |

Submitting for review would mean privacy policies, demo videos and weeks of
waiting, for an app with one user.

---

## Meta — Facebook Page + Instagram

One app covers both. Instagram is only reachable through the Facebook Page it
is linked to.

**Prerequisites**

* A Facebook Page you have an admin role on.
* An Instagram **Business or Creator** account (not personal) linked to that
  Page. Set the account type in the Instagram app under *Settings → Account
  type and tools*, then link it from the Page's *Linked accounts*.

Without the Business/Creator conversion the insights endpoints return nothing —
this is the most common reason an Instagram connection appears to succeed but
produces no metrics.

**Steps**

1. <https://developers.facebook.com/apps/> → **Create app** → type **Business**.
2. Add the **Facebook Login** product.
3. *Facebook Login → Settings → Valid OAuth Redirect URIs* — paste the Meta
   redirect URI from `show_oauth_config`.
4. *App settings → Basic* — copy **App ID** and **App Secret** into
   `META_APP_ID` and `META_APP_SECRET`.
5. Leave the app in **Development** mode. As an app admin you can grant
   `pages_show_list`, `pages_read_engagement`, `instagram_basic` and
   `instagram_manage_insights` to yourself without review.

**Scopes:** `pages_show_list`, `pages_read_engagement`, `instagram_basic`,
`instagram_manage_insights`

**Note:** the integration pins Graph API `v21.0` (`META_GRAPH_API_VERSION`).
If you set the app to a much newer version later, check the Instagram insights
metrics still resolve — Meta retired `impressions` for media created on or
after 2024-04-21 in v22+, which the code already handles by falling back.

---

## Google — YouTube

**Steps**

1. <https://console.cloud.google.com> → create or select a project.
2. *APIs & Services → Library* → enable **both**:
   * YouTube Data API v3
   * YouTube Analytics API
3. *OAuth consent screen* → **External**. Fill in app name and the two email
   fields.
4. Add the two scopes (both are classed "sensitive"):
   `.../auth/youtube.readonly` and `.../auth/yt-analytics.readonly`
5. **Set publishing status to "In production."** See the warning below — this
   one matters more than it looks.
6. *Credentials → Create credentials → OAuth client ID → Web application*.
7. *Authorized redirect URIs* — paste the Google redirect URI from
   `show_oauth_config`.
8. Copy **Client ID** and **Client secret** into `YOUTUBE_CLIENT_ID` and
   `YOUTUBE_CLIENT_SECRET`.

> ### Do not leave the app in "Testing"
>
> Google **expires refresh tokens after 7 days** for apps whose publishing
> status is *Testing*. The connection would work for a week and then silently
> die, needing a manual reconnect — and because the sync catches per-platform
> failures by design, it would fail quietly rather than loudly.
>
> Setting the status to *In production* without verification is fine for a
> personal tool. Refresh tokens then stop expiring. The cost is a "Google
> hasn't verified this app" interstitial on the consent screen: click
> **Advanced → Go to (app name)**. Unverified apps with sensitive scopes are
> capped at 100 users, which is 99 more than this needs.

**Quota:** the integration is built to stay far inside the 10,000 units/day
limit — it never calls `search.list` (100 units), walking the uploads playlist
instead. A full sync costs roughly 20 units.

---

## TikTok

The most restrictive of the three, and the one to attempt last.

**Steps**

1. <https://developers.tiktok.com/apps/> → create an app.
2. Add the **Login Kit** and **Display API** products.
3. In the Login Kit configuration, add the TikTok redirect URI from
   `show_oauth_config`.
4. Copy **Client key** and **Client secret** into `TIKTOK_CLIENT_KEY` and
   `TIKTOK_CLIENT_SECRET`. Note TikTok says *client key*, not client ID — the
   parameter name differs from the other two providers, which is why the
   setting is named differently.
5. Create a **Sandbox** and add your own TikTok account as a target user, so
   you can authorise `user.info.stats` and `video.list` before any review.

**Scopes:** `user.info.basic`, `user.info.stats`, `video.list`

### Domain verification may force a custom domain

TikTok requires you to verify ownership of the redirect URI's domain, by DNS
record or by serving a verification file at the domain root. Neither is
possible on `*.up.railway.app`, which Railway owns and shares across projects.

The fix is to attach a custom domain to the Railway **web** service — for
example `api.worldstories.net` — and use that as the public base URL:

```
EXTRA_ALLOWED_HOSTS=api.worldstories.net
EXTRA_ALLOWED_ORIGINS=https://api.worldstories.net
SOCIAL_ANALYTICS_PUBLIC_BASE_URL=https://api.worldstories.net
```

`ALLOWED_HOSTS` and the CORS/CSRF origin list both read those variables, so no
code change is needed. **Re-register the redirect URI in every console after
changing the base URL** — all three redirect URIs derive from it, so they all
change at once. Run `show_oauth_config` again to get the new strings.

A custom domain is worth doing regardless: it decouples the OAuth registrations
from a Railway-generated hostname you do not control.

---

## After each provider

Set the credentials on the **web** and **worker** services (web builds the
authorise URL and completes the exchange; the worker uses the same credentials
to refresh tokens during sync), redeploy, then:

1. Open the app's **Connections** screen and tap **Connect**.
2. You should land on the provider's consent screen, then on a "Connected"
   confirmation page served by the backend.
3. The platform row should flip to **CONNECTED** with the account name.
4. Trigger a sync. Content and metrics should appear within a minute or two.

A platform whose credentials are not set returns `503 not configured` from its
`oauth-url/` endpoint and shows an error in the app. That is expected, and the
other platforms are unaffected — connect them one at a time.

### If the callback fails

| Symptom | Cause |
| --- | --- |
| "Invalid redirect URI" on the provider's page | The registered URI does not match. Re-run `show_oauth_config` and compare character for character, trailing slash included |
| "Connection cancelled" page | You declined, or the app lacks the scope. Check the app's mode (Development / In production / Sandbox) |
| "This OAuth request expired" | The state row is older than 15 minutes — start the connect again |
| Connected, but no metrics for Instagram | The account is not Business/Creator, or is not linked to the Page |
