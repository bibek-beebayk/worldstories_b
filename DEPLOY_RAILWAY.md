# Railway deployment guide — social media analytics update

This update adds background jobs to a project that previously ran as a single
web container. That means **three new pieces of Railway infrastructure**: a
Redis service, a Celery worker service, and a Celery beat service.

Production backend: `https://worldstories-b-production.up.railway.app`

---

## What changed in the repo

| File | Change | Why it matters for deploy |
| --- | --- | --- |
| `entrypoint.sh` | Runs its arguments if given any, else the web server | Lets worker/beat run from the same image. Backward-compatible — the web service passes no arguments and behaves exactly as before |
| `.dockerignore` | Excludes `.env`, `env/`, `*.log`, `.git/` | Stops the local `.env` (and a 121 MB debug log, and the 453 MB virtualenv) being baked into the image on `railway up` |
| `core/settings/base.py` | Celery config, beat schedule, new env vars | Broker falls back to `REDIS_PRIVATE_URL` → `REDIS_URL` |
| `requirements/base.txt` | `celery`, `redis`, `google-auth-oauthlib`, `google-api-python-client`, `cryptography` | Installed by the existing `prod.txt` (`-r base.txt`) |
| `apps/social_media_analytics/` | New app + one migration | Applied automatically by `entrypoint.sh` |

The Dockerfile is unchanged. Python 3.11 (its base image) compiles the new app
cleanly.

---

## Step 1 — Add Redis

Railway project → **New** → **Database** → **Add Redis**.

The `redis` line in `requirements/base.txt` is only the *client library*. Celery
needs a real broker process, which is what this service provides.

Once attached, Railway exposes `REDIS_URL` and `REDIS_PRIVATE_URL`. Settings
already fall back to those in that order, so **no broker variable is normally
needed**. Set `CELERY_BROKER_URL` explicitly only if private networking gives
trouble.

## Step 2 — Generate the encryption key

Platform OAuth tokens are encrypted at rest and the app refuses to store them
in plaintext, so this is **required** before any platform can be connected:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate a **new key for production** — do not reuse the local development one.
Rotating this key later makes all stored tokens unreadable and every platform
has to be reconnected.

## Step 3 — Set variables on the web service

Add to the existing web service (Variables tab):

```
SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=<the key from step 2>
SOCIAL_ANALYTICS_PUBLIC_BASE_URL=https://worldstories-b-production.up.railway.app
```

Optional, per platform, once each developer app is approved:

```
META_APP_ID=
META_APP_SECRET=
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
```

Platforms left blank simply return `503 not configured` from their
`oauth-url/` endpoint. Everything else in the app works.

> **Tip:** put these in Railway's project-level **Shared Variables** rather than
> on the web service. Variables are per-service, and the worker and beat need
> the same set — shared variables save maintaining three copies.

## Step 4 — Deploy the web service

Push, or redeploy. `entrypoint.sh` runs `manage.py migrate` on boot, so the new
app's migration applies automatically.

Verify:

```bash
curl https://worldstories-b-production.up.railway.app/api/social-media-analytics/health/
# {"status":"ok","service":"social_media_analytics","time":"..."}
```

## Step 5 — Add the worker service

**New** → **GitHub Repo** → same repo → then in Settings:

* **Custom Start Command:** `celery -A core worker -l info --concurrency=2`
* **Networking:** no public domain needed

Required variables (same values as the web service — the worker loads the same
Django settings):

```
SECRET_KEY=            # settings reads this with os.environ[...] and will crash without it
PGDATABASE=
PGUSER=
PGPASSWORD=
PGHOST=
PGPORT=
SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=
```

Plus the R2 and platform credential variables, since `prod.py` reads them at
import time.

Confirm in the logs:

```
Connected to redis://...
celery@... ready.
  . social_media_analytics.sync_meta
  . social_media_analytics.sync_youtube
  . social_media_analytics.sync_tiktok
  . social_media_analytics.refresh_expiring_tokens
  . social_media_analytics.sync_all
```

## Step 6 — Add the beat service

Same again:

* **Custom Start Command:** `celery -A core beat -l info --schedule=/tmp/celerybeat-schedule`
* **Replicas: 1 — never more.** Two beat instances double-enqueue every sync,
  and because snapshots are append-only you get duplicate rows per day rather
  than a visible error.

The `--schedule` path matters: beat writes a schedule file, and the app
directory is not reliably writable in a deployed container.

Confirm in the logs: `beat: Starting...`

## Step 7 — Register the OAuth redirect URIs

In each provider's developer console, using the production base URL:

| Provider | Redirect URI |
| --- | --- |
| Meta | `https://worldstories-b-production.up.railway.app/api/social-media-analytics/oauth/meta/callback/` |
| Google | `https://worldstories-b-production.up.railway.app/api/social-media-analytics/oauth/youtube/callback/` |
| TikTok | `https://worldstories-b-production.up.railway.app/api/social-media-analytics/oauth/tiktok/callback/` |

These must match **character for character**, trailing slash included.

Scopes: Meta needs `pages_show_list`, `pages_read_engagement`,
`instagram_basic`, `instagram_manage_insights`; Google needs `youtube.readonly`
and `yt-analytics.readonly`; TikTok needs `user.info.basic`, `user.info.stats`,
`video.list`.

## Step 8 — Point the app at production

```bash
flutter build apk --release \
  --dart-define=API_BASE_URL=https://worldstories-b-production.up.railway.app
```

---

## Final service layout

| Service | Start command | Replicas |
| --- | --- | --- |
| web | *(default)* | any |
| worker | `celery -A core worker -l info --concurrency=2` | any |
| beat | `celery -A core beat -l info --schedule=/tmp/celerybeat-schedule` | **1** |
| Redis | *(managed)* | 1 |
| Postgres | *(existing)* | 1 |

Syncs run daily at 03:00/03:20/03:40 UTC, with a token refresh pass every six
hours. Tune with `SOCIAL_ANALYTICS_SYNC_HOUR` and
`SOCIAL_ANALYTICS_SYNC_OFFSETS`.

---

## Gotchas

**Without a worker, `POST /sync/` runs inline.** It falls back to syncing in the
web request, which can tie up a gunicorn worker for minutes. That fallback is a
local-development convenience, not a production path — add the worker.

**First deploy has a harmless migration race.** If the worker boots before the
web service finishes migrating, it errors and Railway restarts it. It settles on
its own; deploy web first if you want to avoid the noise.

**Variables are per-service.** A worker missing `SECRET_KEY` or the encryption
key crashes on boot, not at task time. Shared Variables avoid this.

**The result backend is optional.** Nothing polls task results, so you can set
`CELERY_RESULT_BACKEND=` (empty) to stop results accumulating in Redis.

**Redis is a paid add-on** beyond Railway's trial credit, as is running two
extra always-on services. The worker can be scaled to zero between syncs if
cost matters more than on-demand `POST /sync/`.
