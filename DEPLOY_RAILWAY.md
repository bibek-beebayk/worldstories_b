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

Once attached, Railway exposes `REDIS_URL` and `REDIS_PRIVATE_URL` — but **only
on the Redis service itself**. Railway does *not* inject them into your other
services, so each service that talks to the broker needs an explicit reference:

```
CELERY_BROKER_URL=${{Redis.REDIS_URL}}
CELERY_RESULT_BACKEND=${{Redis.REDIS_URL}}
```

`Redis` is the service name as it appears in your project; Railway's variable
editor autocompletes it. Add **both** lines to **web, worker and beat**.

**Which variable name?** Railway has changed this. On the current Redis
template, `REDIS_URL` *is* the private address and `REDIS_PUBLIC_URL` is the
external proxy. Older templates used `REDIS_URL` for the public proxy and
`REDIS_PRIVATE_URL` for the internal one, so use whichever your project offers.

The reliable check is the worker's startup banner rather than the variable
name: a correct value shows `redis.railway.internal`, the private network.
A `*.proxy.rlwy.net` host means you picked the public URL — it works, but
routes over the public internet and bills egress on every queue poll.

Do not copy the `redis://127.0.0.1:6379/0` values out of `.env.example` — those
are local-development defaults. Setting the broker but leaving the result
backend on localhost produces the worst version of this failure: the worker
boots, registers every task and logs `ready.`, but each dispatch dies with
`Retry limit exceeded while trying to reconnect to the Celery result store
backend` and nothing is ever queued — and because the worker never receives the
message, its log shows nothing at all. Check the `results:` line of the startup
banner, not just `transport:`.

Nothing in this app reads task results, so `CELERY_RESULT_BACKEND=` (empty) is
also valid and disables result storage entirely.

If the private URL will not connect — Railway's private network is IPv6-only
and can need a moment after first provisioning — use `${{Redis.REDIS_URL}}`
instead.

The settings fallback chain is `CELERY_BROKER_URL` → `REDIS_PRIVATE_URL` →
`REDIS_URL` → `redis://127.0.0.1:6379/0`. That last entry is the local
development default, and seeing it in a deployed worker's banner means no
broker variable reached the service.

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

Variables — same values as the web service, because the worker imports the same
Django settings module. They differ in *how loudly* they fail:

```
SECRET_KEY=                              # crashes on boot if missing
PGDATABASE= PGUSER= PGPASSWORD= PGHOST= PGPORT=
SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY=
META_APP_ID= META_APP_SECRET=            # only for the platforms you have approved
YOUTUBE_CLIENT_ID= YOUTUBE_CLIENT_SECRET=
TIKTOK_CLIENT_KEY= TIKTOK_CLIENT_SECRET=
```

`SECRET_KEY` is the only one read as `os.environ["..."]`, so it is the only one
that stops the container at boot. **Everything else has a default**, which makes
them more dangerous, not less:

* Missing `PG*` falls back to `127.0.0.1:5432`, so the worker starts happily and
  then fails on every task trying to reach a database that isn't there.
* A missing encryption key raises `ImproperlyConfigured` only when a task first
  touches a token — i.e. at 03:00, not at deploy time.

The R2/S3 variables are **not** needed by the worker: the sync tasks store
thumbnails as URLs, never as uploaded files, so nothing in this app touches
object storage. Harmless to include if you use Shared Variables.

Confirm in the logs. Celery prints a startup banner, then the task list, then
the ready line — all three matter:

```
- ** ---------- .> transport:   redis://...      <- found the broker
- ** ---------- .> results:     redis://...
- *** --- * --- .> concurrency: 2 (prefork)

[tasks]
  . core.celery.debug_task
  . social_media_analytics.refresh_expiring_tokens
  . social_media_analytics.sync_all
  . social_media_analytics.sync_meta
  . social_media_analytics.sync_tiktok
  . social_media_analytics.sync_youtube

[INFO/MainProcess] Connected to redis://...      <- broker reachable
[INFO/MainProcess] mingle: all alone             <- normal with one worker
[INFO/MainProcess] celery@... ready.             <- accepting work
```

If `transport:` says `redis://127.0.0.1:6379/0`, no broker variable reached this
service — add `CELERY_BROKER_URL=${{Redis.REDIS_URL}}` (step 1).
Attaching the Redis service to the project is not enough on its own.

If `results:` says `redis://127.0.0.1:6379/0` while `transport:` looks correct,
`CELERY_RESULT_BACKEND` was left on the local default. The worker will log
`ready.` and then silently receive nothing, because the dispatch fails on the
sender's side. If the five
`social_media_analytics.*` tasks are missing, the app is not in `INSTALLED_APPS`
on this service. If it never reaches `ready.`, it cannot connect to the broker.

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

Full walkthrough per provider: [`OAUTH_SETUP.md`](OAUTH_SETUP.md). Run
`python manage.py show_oauth_config` to print the exact strings this deployment
will send — every provider requires a character-for-character match.

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

## Which variables go on which service

Not every service needs every variable. Beat in particular is just an alarm
clock — it reads the schedule from settings and publishes a task *name* to
Redis; it never opens the database, decrypts a token, or calls a platform API.
(Verified: beat starts cleanly with a deliberately broken `PG*` set and no
encryption key.)

| Variable | web | worker | beat |
| --- | :---: | :---: | :---: |
| `SECRET_KEY` | yes | yes | yes |
| `CELERY_BROKER_URL` | yes | yes | yes |
| `PGDATABASE` `PGUSER` `PGPASSWORD` `PGHOST` `PGPORT` | yes | yes | — |
| `SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY` | yes | yes | — |
| `META_*` / `YOUTUBE_*` / `TIKTOK_*` | yes | yes | — |
| `SOCIAL_ANALYTICS_PUBLIC_BASE_URL` | yes | — | — |
| `R2_*` | yes | — | — |

Web builds the authorize URLs and completes the OAuth exchange; the worker uses
the same credentials to *refresh* tokens during sync.

Practical advice: put `SECRET_KEY`, `CELERY_BROKER_URL` and the `PG*` set in
Shared Variables and reference them everywhere — consistency beats precision,
and three services drifting apart is the worse failure. The one worth leaving
off beat is `SOCIAL_ANALYTICS_FIELD_ENCRYPTION_KEY`: it unlocks live platform
credentials and beat has no use for it.

> If you later switch to `django-celery-beat`'s `DatabaseScheduler` to edit
> schedules from the Django admin, beat *would* then need the database. The
> current setup uses the file-based `PersistentScheduler`.

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
