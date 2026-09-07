# Worldstories backend (`worldstories_b`)

Django + DRF + PostgreSQL backend for Worldstories.

```bash
python -m venv env && source env/bin/activate
pip install -r requirements/dev.txt
cp .env.example .env        # then fill in the values
python manage.py migrate
python manage.py runserver
```

Settings live in `core/settings/` (`base.py` plus `dev.py` / `prod.py`, selected
through `core/settings/env.py`, which is gitignored).

---

## Social media analytics app

`apps/social_media_analytics` tracks content performance across a **Facebook
Page, Instagram, TikTok and YouTube** and serves it to the Flutter Android app
in [`../app`](../app). It is a personal, single-user tool.

It is deliberately **separate from `apps.stats`**, which owns Worldstories' own
on-site analytics at `/api/analytics/` and `/api/admin/analytics/`. Nothing in
this app touches those models or routes.

### Architecture

The backend holds every platform credential. The Flutter app authenticates
against Django only and never sees a Meta/Google/TikTok token:

```
Flutter app --(JWT)--> Django ----> Meta Graph / YouTube Data+Analytics / TikTok Display
                          |
                    Celery beat --> PostgreSQL (time-series snapshots)
```

### Layout

| Path | What it is |
| --- | --- |
| `models.py` | `PlatformConnection`, `ContentItem`, `MetricSnapshot`, `AccountSnapshot`, `OAuthState`, `SyncRun` |
| `fields.py` | Fernet-encrypted model field for stored tokens |
| `integrations/` | `meta.py`, `youtube.py`, `tiktok.py` behind the shared interface in `base.py` |
| `services.py` | OAuth completion and snapshot writing, shared by the API and the tasks |
| `tasks.py` | Celery sync jobs |
| `api/` | DRF serializers, views and `urls.py` |
| `management/commands/seed_social_analytics.py` | Mock data for local development |

### Setup

1. **Migrations** — the app ships one initial migration:

   ```bash
   python manage.py migrate social_media_analytics
   ```

2. **Encryption key** (required — platform tokens are never stored in
   plaintext):

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Put the result in `SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY`. Rotating this key
   makes existing stored tokens unreadable and every platform has to be
   reconnected.

3. **Provider credentials** — see the `social media analytics` block appended
   to `.env.example` for the full list. Each provider needs its redirect URI
   registered in its own developer console, matching the backend exactly:

   | Provider | Redirect URI | Scopes |
   | --- | --- | --- |
   | Meta | `/api/social-media-analytics/oauth/meta/callback/` | `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_manage_insights` |
   | Google | `/api/social-media-analytics/oauth/youtube/callback/` | `youtube.readonly`, `yt-analytics.readonly` |
   | TikTok | `/api/social-media-analytics/oauth/tiktok/callback/` | `user.info.basic`, `user.info.stats`, `video.list` |

   Providers require HTTPS on a publicly reachable host, so use a tunnel in
   development and set `SOCIAL_ANALYTICS_PUBLIC_BASE_URL` to it.

### Celery

This app introduced Celery to the project; there was no broker before it. The
app is `core/celery.py`, loaded from `core/__init__.py`, and every setting lives
in `core/settings/base.py` under the `CELERY_` namespace — **do not stand up a
second Celery app** for future background work, add tasks to this one.

```bash
celery -A core worker -l info
celery -A core beat -l info
```

Task names and default schedule (`CELERY_BEAT_SCHEDULE`, all times UTC):

| Task | Default schedule | What it does |
| --- | --- | --- |
| `social_media_analytics.sync_meta` | daily 03:00 | Facebook Page + Instagram |
| `social_media_analytics.sync_youtube` | daily 03:20 | YouTube channel + videos |
| `social_media_analytics.sync_tiktok` | daily 03:40 | TikTok videos |
| `social_media_analytics.refresh_expiring_tokens` | every 6h at :10 | Proactive credential refresh |
| `social_media_analytics.sync_all` | on demand | Everything; also what `POST /sync/` runs |

Tune with `SOCIAL_ANALYTICS_SYNC_HOUR` and `SOCIAL_ANALYTICS_SYNC_OFFSETS`
(`meta,youtube,tiktok` minutes past the hour). Each task is idempotent — a run
only ever *appends* snapshot rows, so a manual re-run after a failure is safe.
A failure on one platform (or one connection) is recorded on its `SyncRun` and
never aborts the others.

### Deploying (Railway)

> Full step-by-step guide: [`DEPLOY_RAILWAY.md`](DEPLOY_RAILWAY.md)

The `redis` line in `requirements/base.txt` is only the **client library** — it
speaks the protocol, it is not a server. Celery needs a real broker process, so
a Redis service has to exist alongside the app.

Three services from this one repo/image:

| Service | Start command | Notes |
| --- | --- | --- |
| web | *(none — the default)* | migrate, collectstatic, gunicorn |
| worker | `celery -A core worker -l info --concurrency=2` | executes the sync tasks |
| beat | `celery -A core beat -l info --schedule=/tmp/celerybeat-schedule` | **exactly one instance**, never scale past 1 |
| Redis | *(Railway's Redis service)* | the broker |

`entrypoint.sh` runs its arguments when given any, and falls back to the web
server when given none — that is what lets the worker and beat run from the
same image via Railway's custom start command.

`CELERY_BROKER_URL` falls back to `REDIS_PRIVATE_URL` then `REDIS_URL`, both of
which Railway injects once a Redis service is attached, so no manual broker
variable is normally needed. If private networking gives trouble, set
`CELERY_BROKER_URL` explicitly to the public `REDIS_URL`.

Beat's schedule file is put in `/tmp` because the app directory is not reliably
writable in a deployed container. Two beat instances would double-run every
sync, so keep that service at one replica.

Without a worker running, `POST /sync/` falls back to syncing **inline in the
web request**, which can tie up a gunicorn worker for minutes. It is a
development convenience, not a production path.

### API

Everything is under `/api/social-media-analytics/`, JWT-authenticated with the
project's existing `djangorestframework-simplejwt` setup.

```
GET  health/
POST auth/login/                          # NB: the field is "email" — User.USERNAME_FIELD
POST auth/refresh/
GET  connections/
GET  connections/sync-status/
GET  connections/{platform}/oauth-url/
POST connections/{platform}/disconnect/
GET  content/?platform=&from=&to=&sort=&q=&page=
GET  content/{id}/
GET  content/{id}/history/
GET  dashboard/summary/?days=
GET  dashboard/account-trends/?platform=&days=
POST sync/
GET  oauth/{provider}/callback/           # browser redirect target, not for the app
```

Note on the dashboard: `views_this_period` is a **delta**, not a lifetime
total. Every platform reports cumulative per-post counters, so the backend
diffs each item's newest snapshot in the period against its newest snapshot
before it. Summing raw counters across periods would double-count.

### Local development without provider credentials

Platform developer apps need review before real credentials are issued. To
exercise the whole flow meanwhile:

```bash
python manage.py createsuperuser
python manage.py seed_social_analytics --days 60 --items-per-platform 12
```

This writes mock connections, content and snapshot history through the real
models, so the dashboard → content list → content detail → trend chart flow
works end to end. `--clear` removes it again; it never touches real synced rows.

### Tests

```bash
python manage.py test apps.social_media_analytics
```

### Platform caveats worth knowing

* **TikTok** exposes no historical or demographic endpoint at this tier — only
  current per-video counters. All TikTok history in the app comes from our own
  repeated polling, so sync cadence directly determines data quality.
* **YouTube** has a 10,000 unit/day quota. The integration never calls
  `search.list` (100 units); it walks the uploads playlist and batches
  `videos.list` instead, so a full sync costs roughly 20 units.
* **Meta** long-lived user tokens last ~60 days and have no refresh grant —
  `refresh_expiring_tokens` re-exchanges them before expiry. Instagram
  `impressions` was replaced by `views` for media created after 2024-04-21, so
  one of those two columns is null depending on the post's age.
* Not every platform reports every metric; nulls in `MetricSnapshot` mean
  "unavailable here", never zero. The per-platform table is in that model's
  docstring.
