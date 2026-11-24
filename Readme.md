# WorldStories backend

## Weekly newsletter

The weekly digest uses double opt-in for anonymous addresses. Existing users are not subscribed automatically.

### Environment variables

- `RESEND_API_KEY`: enables Anymail's Resend backend. When absent, the existing SMTP backend and its `EMAIL_HOST_*` settings remain active.
- `NEWSLETTER_FROM_EMAIL`: required for real sends, for example `WorldStories <digest@worldstories.net>`.
- `NEWSLETTER_REPLY_TO`: optional reply address. When set, it is also included as a `mailto:` List-Unsubscribe option.
- `NEWSLETTER_SITE_URL`: public frontend origin without a trailing slash, for example `https://worldstories.net`.
- `NEWSLETTER_API_URL`: public backend origin without a trailing slash, for example `https://api.worldstories.net`.
- `NEWSLETTER_POSTAL_ADDRESS`: required physical postal address shown in every digest footer.
- `NEWSLETTER_SEND_DELAY_SECONDS`: delay between recipients; defaults to `0.6`.
- `NEWSLETTER_DAILY_SEND_LIMIT`: maximum digest emails sent per day across all runs; defaults to `80`.
- `NEWSLETTER_SEND_TIMEZONE`: IANA timezone used for weekday cohort selection and daily caps; defaults to `UTC`.

Send one test copy without creating an issue or delivery record:

```bash
python manage.py send_weekly_digest --to you@example.com
```

Preview the selection and plain-text rendering without sending:

```bash
python manage.py send_weekly_digest --dry-run
```

## Story maintenance commands

Backfill `Story.cached_summary_reading_minutes` after deploy (safe to run more than once):

```bash
python manage.py backfill_summary_minutes
```

### Railway schedule

Create a separate Railway cron service using the same application image and environment variables. Its command is:

```bash
python manage.py send_weekly_digest
```

Run the cron **DAILY** (for example `0 9 * * *`, which runs at 09:00 UTC). Railway cron expressions run in UTC. Do not run the command from the web service startup path.

Each confirmed subscriber is assigned a weekday cohort (`0` = Monday through `6` = Sunday, balanced across the week) so every subscriber still receives one digest per week while sends are spread evenly across the week. Each daily run sends to overdue subscribers first, then today's weekday cohort, up to `NEWSLETTER_DAILY_SEND_LIMIT`. Anyone left over is picked up on the next day's run.

### Sending-domain DNS

Verify the sending domain in Resend and publish the exact records it supplies:

- SPF TXT record authorizing Resend for the envelope-sender domain.
- DKIM records supplied by Resend for message signing.
- DMARC TXT record at `_dmarc`, beginning with a monitoring policy such as `v=DMARC1; p=none;` and an aggregate-report address you monitor. Tighten the policy after SPF and DKIM alignment are confirmed.

DNS hostnames and verification values are account/domain-specific; copy them from the Resend domain-verification screen rather than using example values.

## Admin audiobooks

Superuser-only endpoints for managing standalone and story-linked audiobooks, tracks, and series:

- `GET/POST /api/admin/audiobooks/` — list (filters: `q`, `story_link=all|linked|standalone`, `series`, `series_filter=none`, `exclude_series=<id>`, `status=published|draft|scheduled`, `ordering`) and create audiobooks (accepts `series_items: [{series_id, order?}]`). Standalone audiobooks require `title`, `author`, `language`, and `about`.
- `GET/PATCH/DELETE /api/admin/audiobooks/<id>/` — retrieve (with `effective` fallback fields, ordered `tracks`, and `series` memberships), update (supports `remove_cover=true` and `series_items`), or delete (cascades tracks, transcripts, cues, and listener progress; leaves linked `Story` intact).
- `POST /api/admin/audiobooks/bulk-series/` — bulk add or remove selected audiobooks (max 200) to/from up to 20 series (`{ids, series_ids, mode: "add"|"remove"}`), returns `{added, removed, skipped}`.
- `GET /api/admin/audiobooks/stats/` — summary counts (`total`, `standalone`, `series_count`, `missing_duration`).
- `GET /api/admin/audiobooks/story-search/?q=` — search up to 10 stories (published or draft) with `audiobook_count`.
- `POST /api/admin/audiobooks/<id>/tracks/` — multi-file `.mp3` upload (`files`), derives titles from filenames, probes duration/size, skips invalid files and returns `{created, errors}`.
- `POST /api/admin/audiobooks/<id>/tracks/reorder/` — atomic reorder by exact track ID list (`{ids: [...]}`).
- `PATCH/DELETE /api/admin/audiobooks/<id>/tracks/<track_id>/` — rename, replace audio file, or delete track (closes order gap and refreshes cache).
- `POST /api/admin/audiobooks/<id>/tracks/<track_id>/reread-duration/` — re-probes duration from stored audio file.
- `POST /api/admin/audiobooks/<id>/tracks/<track_id>/move/` — moves track (with transcript, cues, and progress preserved) to another audiobook (`{audiobook_id}`) and refreshes both caches.
- `GET/POST/PATCH/DELETE /api/admin/audiobook-series/` — CRUD for audiobook series; `GET /api/admin/audiobook-series/<id>/` returns ordered `audiobooks` from `AudiobookSeriesItem`. Deleting a series removes its `AudiobookSeriesItem` rows and keeps audiobooks intact.
- `POST /api/admin/audiobook-series/<id>/add-audiobooks/` — append audiobooks (`{audiobook_ids}`), skips duplicates, returns `{added, skipped}`.
- `POST /api/admin/audiobook-series/<id>/remove-audiobooks/` — remove audiobooks (`{audiobook_ids}`) and renumber remaining items `1..n`, returns `{removed}`.
- `POST /api/admin/audiobook-series/<id>/reorder/` — atomic reorder by exact audiobook ID list (`{ids: [...]}`).

## Public audiobooks & progress (Phase 3)

- `GET/PUT /api/audio-progress/audiobook/<audiobook_slug>/` — authenticated audiobook-keyed listening progress (accepts `audio_id` or `audio_slug`). On `GET`, if no audiobook-keyed row exists yet but a legacy story-keyed row (`audiobook=None`) exists for a track of the linked story, returns the legacy row so listeners keep their place; the next `PUT` upgrades/writes the audiobook-keyed row. Standalone audiobooks (`story=None`) save and reload cleanly.
- `GET /api/audio-progress/<story_slug>/` — legacy story-keyed progress endpoint remains unchanged (to be deprecated/removed in Phase 4).
- `GET /api/audiobooks/overview/` — `continue_listening` merges audiobook-keyed and legacy rows (audiobook-keyed wins), deduplicated per audiobook.
- `GET /api/audiobooks/stats/`, `GET /api/audiobooks/<slug>/read-along/<audio_slug>/`, and `GET /api/audiobooks/<slug>/tracks/<audio_slug>/stream/` — public stats, read-along transcript/cue payload, and byte-range stream proxy for both standalone and story-linked audiobooks.
- `GET /api/sitemap.xml` — includes every published playable audiobook once at `/listen/<audiobook.slug>`.
- **Phase 4 (current state)**:
  - `AudioReadingProgress` uses `audiobook` as the canonical key with `UniqueConstraint(fields=["user", "audiobook", "audio"], condition=Q(audiobook__isnull=False))` (`stats.0033`). New rows do not write the legacy `story` column.
  - `GET/PUT /api/audio-progress/audiobook/<audiobook_slug>/` and `AudiobookViewSet.overview` `continue_listening` read audiobook-keyed rows only.
  - `GET/PUT /api/audio-progress/<story_slug>/` is a thin compatibility wrapper that logs a deprecation warning and delegates to the story's first published audiobook.
  - `Audio` enforces `UniqueConstraint(fields=["audiobook", "order"])` (`story.0106`).
  - Analytics (`completion.py`, `overview_api.py`, `users_api.py`, `analytics_api.py`, and `AudiobookViewSet._order_by_popular`) aggregate by `audiobook` / `audiobooks`.
  - **Management commands**:
    - `python manage.py audit_audiobooks` — read-only integrity audit (exits 1 on any violation; run before and after backfills and before schema tightening).
    - `python manage.py backfill_audiobooks [--dry-run]` — creates missing `Audiobook` rows for legacy story `Audio` tracks and attaches them.
    - `python manage.py backfill_audio_progress [--dry-run]` — moves `AudioReadingProgress` rows with `audiobook IS NULL` onto audiobook keys (batches of 500, idempotent).
    - `python manage.py recompute_audiobook_totals [--dry-run]` — recomputes `track_count` and `total_duration_seconds` on all audiobooks (batches of 500, idempotent).
    - `python manage.py backfill_series_items [--dry-run]` — copies legacy `Audiobook.series` / `series_order` into `AudiobookSeriesItem` rows (batches of 500, idempotent). Legacy `Audiobook.series` and `Audiobook.series_order` fields are deprecated and will be dropped in a follow-up migration once `backfill_series_items` and `audit_audiobooks` confirm zero missing pairs in production.
