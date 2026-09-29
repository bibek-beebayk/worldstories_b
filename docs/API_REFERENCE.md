# WorldStories API Reference

Generated for building the WorldStories Flutter mobile client (Android first, iOS later) against the existing Django REST Framework backend, with no backend changes. Extracted directly from source (`core/urls.py`, `apps/story/*`, `apps/users/*`, `apps/stats/*`, `core/settings/base.py`, `core/libs/pagination.py`) — there is no OpenAPI/Swagger/Postman doc in the repo, so this file is the source of truth going forward and should be kept up to date as the API evolves.

---

## 1. Authentication & Base Setup

**Base URL pattern:** all endpoints are mounted under `/api/`, e.g. `https://<host>/api/stories/`.

**Auth scheme:** JWT via `rest_framework_simplejwt`. Send the access token as:

```
Authorization: Bearer <access_token>
```

`DEFAULT_AUTHENTICATION_CLASSES` = `rest_framework_simplejwt.authentication.JWTAuthentication` only (no session auth in the default API auth stack — `api/session-auth/` exists separately for the browsable API). `DEFAULT_PERMISSION_CLASSES` defaults to `AllowAny` — each view/viewset opts into `IsAuthenticated`/`IsSuperUser` individually, so treat every endpoint below as public unless stated otherwise.

**Token lifetimes** (`SIMPLE_JWT` in `core/settings/base.py` — only these four keys are overridden, everything else is simplejwt's library default, e.g. `AUTH_HEADER_TYPES = ("Bearer",)`, `ALGORITHM = "HS256"`):
- `ACCESS_TOKEN_LIFETIME`: 60 minutes
- `REFRESH_TOKEN_LIFETIME`: 30 days
- `ROTATE_REFRESH_TOKENS`: True — every refresh call returns a **brand-new** refresh token, and the used one is blacklisted. The client must persist the rotated refresh token from each response, not reuse the original.
- `BLACKLIST_AFTER_ROTATION`: True

**Refresh flow:**
```
POST /api/auth/refresh/
Body: { "refresh": "<refresh_token>" }
Response: { "access": "<new_access_token>", "refresh": "<new_refresh_token>" }  (rotation enabled)
```
(Standard `rest_framework_simplejwt.views.TokenRefreshView`.)

**Logout:**
```
POST /api/auth/logout/
Body: { "refresh": "<refresh_token>" }
Response: 200 { "detail": "Logged out." }
```
Blacklists the refresh token server-side. Missing/invalid tokens still return 200 (idempotent from the client's perspective).

**Pagination shape** — CUSTOM, not stock DRF (`core.libs.pagination.PageNumberPagination`, `DEFAULT_PAGINATION_CLASS`, `PAGE_SIZE = 20` default unless a view sets its own `pagination_class`):

```json
{
  "pagination": {
    "count": 123,
    "page": 2,
    "pages": 7,
    "previous": "https://.../?page=1",
    "next": "https://.../?page=3",
    "size": 20
  },
  "results": [ ... ],
  "aggregate": { ... }   // optional, only present when the view sets it (e.g. library-shelves' total_stories)
}
```
No `count`/`next`/`previous`/`results` flat shape as in stock DRF — everything is nested under `pagination` except `results`/`aggregate`. No `page_size_query_param` is set anywhere, so clients cannot request a different page size via query param; each endpoint's page size is fixed by its own `pagination_class` (see per-endpoint page sizes below: Story catalogue = 12, Author list = 24, Blog = 12, Library shelf = 4, admin story-items = 10000, search sub-lists = 12 each with distinct query params `author_page`/`chapter_page`).

**Error format** — no custom `EXCEPTION_HANDLER` is configured anywhere in the codebase; DRF's default exception handler is used as-is:
- 400 (validation): `{"field_name": ["error message", ...], ...}` or `{"detail": "message"}` for non-field errors
- 401: `{"detail": "message"}` (+ `WWW-Authenticate` header)
- 403: `{"detail": "message"}`
- 404: `{"detail": "Not found."}` or a custom message, e.g. `{"detail": "Progress not found."}`
- 429 (throttled): `{"detail": "Request was throttled. Expected available in N seconds."}`
- 500: not handled by DRF's handler (returns `None`), falls through to Django's own 500 handling — no guaranteed JSON shape.

**Throttling:** `DEFAULT_THROTTLE_CLASSES` is empty globally. Only three endpoints throttle explicitly:
- `POST /api/auth/admin-login/` and the disabled `login` action: `ScopedRateThrottle`, scope `"login"`, rate **5/min**, IP-keyed.
- `POST /api/analytics/events/`: `AnalyticsEventThrottle` (custom `SimpleRateThrottle`), **120/min**, keyed by user id if authenticated else IP.
- `POST /api/nepalikatha/events/`: similar throttle, scope `"nepalikatha-events"`, **120/min**, IP-keyed only.

---

## 2. Auth Endpoints

Base path: `/api/auth/` (DRF router, `AuthenticationViewSet`, basename `auth`).

> **Important:** Email/password registration, OTP verification, and email/password login are all **disabled** — each returns `410 Gone` with `{"message": "... Use Google login."}`. The only real sign-in path is Google login. `RegisterSerializer`, `LoginSerializer`, `OTPValidateSerializer`, `OTPResendSerializer` exist in code but are effectively dead — their endpoints below all just 410.

### `POST /api/auth/register/` — disabled (410)
### `POST /api/auth/validate-otp/` — disabled (410)
### `POST /api/auth/resend-otp/` — disabled (410)
### `POST /api/auth/login/` — disabled (410)

### `POST /api/auth/google-login/` — Public
Request body:
```json
{ "token": "<Google ID token>" }
```
Verifies the token against `GOOGLE_CLIENT_ID`. Creates the user (`get_or_create` by email) if it doesn't exist, with a unique auto-generated username. Rejects deactivated users (403). Marks `otp_verified = True`.

Response 200:
```json
{
  "access": "<jwt access token>",
  "refresh": "<jwt refresh token>",
  "user": {
    "id": 1,
    "email": "user@example.com",
    "name": "Display Name or username",
    "is_first_login": true
  }
}
```
Errors: `400 {"error": "Token missing"}`, `400 {"error": "Invalid Google token"}`, `500 {"error": "Google OAuth is not configured."}`, `403 {"error": "This account has been deactivated."}`.

### `POST /api/auth/admin-login/` — Public, throttled 5/min
Email+password login, but **requires `is_superuser`**. Not useful for the mobile app (superuser only). Body: `{"email":, "password":}`. Response mirrors google-login's token shape plus `user.is_superuser`.

### `GET /api/auth/username-available/?username=<value>` — Requires auth
Response: `{"available": true|false}` (excludes the caller's own row). `400 {"available": false, "detail": "Username is required."}` if missing.

### `GET/PATCH /api/auth/me/` — Requires auth
**GET** response (`UserProfileSerializer`):
```json
{
  "id": "uuid",
  "email": "string",
  "username": "string",
  "is_superuser": false,
  "display_name": "string|null",
  "bio": "string|null",
  "avatar_url": "string|null",
  "date_joined": "ISO datetime",
  "favorites_count": 0,
  "reviews_count": 0,
  "reading_in_progress_count": 0,
  "listening_in_progress_count": 0,
  "watching_in_progress_count": 0,
  "preferred_genres": [ { "id":, "name":, "slug":, "description":, "stories_count": } ]
}
```
**PATCH** body (`UserProfileUpdateSerializer`, all optional, partial): `username` (string, validated unique), `display_name`, `bio`, `avatar_url`, `preferred_genres` (array of Genre ids). Returns the updated `UserProfileSerializer` payload.

---

## 3. Story Content Endpoints

### Story catalogue — `GET /api/stories/` (list) and `GET /api/stories/{slug}/` (retrieve)
Public read-only (`StoryViewSet`, `ReadOnlyModelViewSet`). Pagination: `CataloguePagination`, page size **12**.

**Query params (via `StoryFilter`, `django_filters`):**
| Param | Type | Behavior |
|---|---|---|
| `status` | `completed`\|`ongoing` | filters `is_completed` |
| `genres` | comma-separated Genre ids | `genres__id__in` |
| `categories` | comma-separated Category ids | `categories__id__in` |
| `language` | code (`en`,`es`,`fr`,`de`,`pt`,`it`,`hi`,`ne`,`ja`,`ko`,`zh`,`ar`,`ru`) or `all` | filters `language` |
| `story_type` | StoryType id or `all` | filters `story_type_id` |
| `country` | ISO alpha-2 or `all` | filters `country` (uppercased) |
| `has_audio` | `true`\|`false` | audios exist/not |
| `has_video` | `true`\|`false` | videos exist/not |
| `has_summary` | `true`\|`false` | non-blank `summary` |
| `has_read_along` | `true`\|`false` | some audio has both file + transcript |
| `is_original` | `true`\|`false` | `is_original` field |
| `show_in_nepali_site` | `true`\|`1` | narrows to Nepali-site subset (also narrows translation-collapsing) |
| `moods` | comma-separated mood slugs | only publicly-visible mood assignments |
| `sort` | `recent`\|`popular`\|`rating`\|`views` | ordering (note: `popular` is actually bugged to sort ascending by `site_published_date` — a known TODO in the backend code) |
| `q` | free text | matches title/about/author name/genre/category/tag name |

List response items use `StoryListSerializer` (card shape):
```json
{
  "id": 1, "title": "", "slug": "",
  "story_type": "Novel",
  "language": "en",
  "site_published_date": "YYYY-MM-DD|null",
  "cover_image": "https://.../480x640/...|null",
  "rating": 4.5, "views": 120,
  "has_audio": true, "has_video": false, "has_read_along": true,
  "genres": ["Fantasy", "Adventure"],       // first 2 only, names not objects
  "categories": ["Category A"],              // first 2 only
  "author": "Author Name|null",
  "summary_reading_minutes": 3,
  "reading_time_minutes": 42,
  "reviews_count": 5,
  "is_favorite": false,
  "favorites_count": 12,
  "is_original": false
}
```
Note: `genres`/`categories` here are just name strings, truncated to first 2 — full lists only appear on the detail endpoint.

**Retrieve** (`GET /api/stories/{slug}/`) uses `StoryDetailSerializer`:
```json
{
  "id":, "title":, "slug":, "about":, "summary": "<html>", "retrospective": "<html>",
  "genres": [{"id","name","slug","description","stories_count"}],
  "categories": [ ... same shape ... ],
  "story_type": "Novel",
  "language": "en",
  "translations": [ {"id","slug","language","title"} ],   // sibling editions in same translation_group
  "author": {"id","name","bio","image","stories_count"} ,
  "submitted_by": {"id","email","username","display_name"} | null,
  "original_published_year": 1900, "original_published_month": null, "original_published_day": null,
  "site_published_date": "YYYY-MM-DD",
  "published_date_label": "March 3, 1900",
  "cover_image": "https://.../900x1200/...",
  "pdf_file": "https://...|null", "epub_file": "https://...|null",
  "pdf_size_bytes": 0, "epub_size_bytes": 0,
  "is_completed": true, "is_original": false,
  "tags": [{"id","name","slug","description","stories_count"}],
  "themes": [ ... same shape ... ],
  "rating": 4.5, "views": 120,
  "reviews_count": 5, "is_favorite": false, "favorites_count": 12,
  "chapter_count": 20,
  "chapters": [{"id","title","order","slug","download_size_bytes"}],   // no chapter body here!
  "audios": [ AudioSerializer objects — see §4 ],
  "videos": [{"id","title","slug","youtube_id","order","duration_seconds","aspect_ratio"}],
  "has_audio": true, "has_video": false,
  "reading_time_minutes": 42, "listening_time_minutes": 55, "watch_time_minutes": null,
  "similar_stories": [ StoryListSerializer x up to 6 ]
}
```
Important: `chapters` here is a **list without body content** — fetch actual chapter text via the chapter action below.

**`GET /api/stories/{slug}/chapters/{chapter_slug}/?type=text|audio`** — returns `ChapterSerializer` (`{id,title,order,content,slug}` with full HTML `content`) for `type=text`, or `AudioSerializer` for `type=audio`.

**`GET /api/stories/{slug}/read-along/{audio_slug}/`** — dedicated read-along payload; see §4 for the full shape.

**`GET/POST/DELETE /api/stories/{slug}/reactions/`** — GET is public; POST/DELETE require auth. POST toggles a reaction (`reaction_type` ∈ `loved|funny|surprising|emotional|thought_provoking`) — sending the same type again removes it.
```json
{ "reactions": [{"type":"loved","label":"Loved it","count":10}, ...], "total": 42, "my_reaction": "loved"|null }
```

**`GET /api/stories/surprise/`** — Public, one random recommended story. Query params: `exclude` (slug to exclude), `max_minutes` (int). Response: `{"story": StoryListSerializer|null}`.

**`POST /api/stories/{slug}/view/`** — Public, fire-and-forget view beacon (204 No Content). Bot/IP-deduped (24h window).

**`GET/POST /api/stories/{slug}/reviews/`** — GET public, paginated (`ReviewSerializer`); POST requires auth, one review per user per story:
```json
// ReviewSerializer (read)
{ "id":, "user": {"id","email","username"}, "rating": 1-5, "comment":, "created_at":, "updated_at": }
// ReviewWriteSerializer (write body)
{ "rating": 1-5, "comment": "text" }
```

**`GET/PATCH/DELETE /api/stories/{slug}/reviews/me/`** — requires auth, the caller's own review.

**`POST/DELETE /api/stories/{slug}/favorite/`** — requires auth. Response: `{"is_favorite": bool, "favorites_count": int}`.

**`GET /api/stories/{slug}/because-finished/`** — requires auth. Personalized "because you finished X" — `StoryListSerializer` array.

**`GET /api/stories/{slug}/completion/`** — public. End-of-story screen data:
```json
{
  "story_slug":, "story_title":, "country": "JP"|null, "country_name": "Japan"|null,
  "primary": StoryListSerializer|null,
  "sections": [ {"key":, "title":, "stories": [StoryListSerializer...]} ]
}
```

**`GET /api/stories/{slug}/pdf-stream/`**, **`GET /api/stories/{slug}/epub-stream/`** — binary file streaming (`application/pdf`, `application/epub+zip`).

**`GET /api/stories/{slug}/audios/{audio_slug}/stream/`** — byte-range audio streaming (Safari-compatible `Range` header support), served either directly from S3/R2 or local storage.

### Authors — `GET /api/authors/` (paginated, size 24), `GET /api/authors/{id}/`
`AuthorSerializer`: `{id,name,bio,image,stories_count}`. Detail adds `stories: [StoryListSerializer...]`.

### Tags — `GET /api/tags/`, `GET /api/tags/{slug}/` (unpaginated)
`TagSerializer`: `{id,name,slug,description,stories_count}`. Detail adds `stories`. Only tags with ≥1 published story are listed.

### Themes — same shape as Tags, `/api/themes/`.

### Genres — `GET /api/genres/`, `GET /api/genres/{slug}/` (unpaginated)
Same shape pattern. Query param `show_in_nepali_site=true` narrows the count/visibility.

### Categories — same shape, `/api/categories/`.

### Story types — `GET /api/story-types/` (unpaginated, public)
`{id, name, stories_count}`.

### Home — `GET /api/home/` (public)
```json
{
  "featured_stories": [FeaturedStorySerializer...],   // StoryListSerializer + about, country; manual picks or top-by-views fallback, up to 5
  "daily_story": { "date":, "story": FeaturedStorySerializer, "featured_reason":, "configured": bool } | null,
  "weekly_spotlight": [StoryListSerializer... x6],
  "new_trending": [StoryListSerializer... x5],
  "more_to_explore": [StoryListSerializer... x12],
  "quick_reads": [StoryListSerializer... x8],           // has a summary
  "originals": [StoryListSerializer... x12],
  "tabs": { "recommended": [...x6], "popular": [...x6], "new": [...x6] },
  "sidebar": {
    "recommended": [...x3],
    "stats": { "creators": int, "stories": int, "readers": int }
  }
}
```

### Trending — `GET /api/trending/` (public, cached 60s, varies on `Authorization`)
```json
{
  "most_viewed": [...x10], "highest_rated": [...x10],
  "most_favorited": [...x10], "most_discussed": [...x10]
}
```
(each a `StoryListSerializer` array)

### Discover — `GET /api/discover/` (public, cached 60s)
```json
{
  "genres": [GenreSerializer...], "categories": [CategorySerializer...],
  "story_types": [StoryTypeSerializer...],
  "languages": [{"value":"en","label":"English","stories_count":123}, ...],
  "most_viewed": [...x10], "highest_rated": [...x10],
  "most_favorited": [...x10], "most_discussed": [...x10],
  "new_releases": [...x20], "hidden_gems": [...x20]
}
```

### Story map — `GET /api/story-map/` (public)
```json
{
  "countries": [ {"code":"JP","name":"Japan","stories_count":12}, ... ],
  "total_stories": int, "countries_count": int, "max_stories_count": int
}
```

### Moods — `GET /api/moods/` (public)
```json
{ "moods": [ {"id","slug","name","icon","description","stories_count"}, ... ] }
```
`stories_count` only counts admin-set or reviewed AI mood assignments.

### Journeys — `GET /api/journeys/` (list, public/personalized if authenticated)
```json
{ "journeys": [ {"slug","title","description","type","cover_image","completed":int,"total":int,"is_complete":bool}, ... ] }
```
`type` ∈ `curated|country|genre|theme`. Progress is 0/0 for anonymous users.

### Journey detail — `GET /api/journeys/{slug}/`
```json
{
  "slug","title","description","type","cover_image",
  "completed": int, "total": int, "is_complete": bool,
  "items": [ {"position","required","completed":bool,"story": StoryListSerializer}, ... ]
}
```
404s if the journey has no items or isn't active.

### Search — `GET /api/search/?q=<term>&sort=popular|recent|rating&language=<code>`
Public, three independently-paginated result sets:
```json
{
  "titles": { "pagination": {..., page_query_param default "page"}, "results": [StoryListSerializer...] },
  "authors": { "pagination": {..., page_query_param "author_page"}, "results": [AuthorSerializer...] },
  "chapters": { "pagination": {..., page_query_param "chapter_page"}, "results": [ChapterSearchResultSerializer...] }
}
```
`ChapterSearchResultSerializer`: `{story_slug, story_title, story_cover_image, chapter_slug, chapter_title, excerpt}` — excerpt is centered on the match. Page sizes: titles 12, authors 12, chapters 12.

### Library shelves — `GET /api/library-shelves/` (public, "shelves" = genres)
Pagination: `LibraryShelfPagination`, page size **4**.
```json
{
  "pagination": {...},
  "results": [
    { "id":, "name":, "stories_count":, "preview_stories": [StoryListSerializer... x8] }
  ],
  "aggregate": { "total_stories": int }
}
```

### Blog — `GET /api/blog/` (list, paginated size 12), `GET /api/blog/{slug}/`
Public. `search` query param via `SearchFilter` (`title`, `excerpt`). Extra query params: `sort=oldest` (default newest-first), `linked_to_story=true|false`, `linked_story=<slug>`.
`BlogSerializer`:
```json
{
  "id","title","slug","excerpt","content": "<html>",
  "cover_image": "https://.../1200x630/...",
  "author_name":,
  "linked_stories": [{"id","slug","title","cover_image","author","story_type","language"}],
  "linked_blogs": [{"id","slug","title","excerpt","cover_image","author_name","published_at"}],
  "published_at": "ISO datetime", "updated_at": "ISO datetime"
}
```

### Submissions — `/api/submissions/` (auth required, `ModelViewSet`, own submissions only unless staff)
`SubmissionSerializer` fields: `id, title, about, content, story_type (name, SlugRelated), language, genres (ids), cover_image, cover_image_file, notes, pdf_file, epub_file, status (read-only), reviewer_notes (read-only), published_story (read-only), created_at, updated_at, user_email (read-only)`. PATCH only allowed while `status == "requires_edit"` for non-staff, and resets status back to `pending`. DELETE blocked once `status == "approved"` for non-staff.

---

## 4. Audio / Video / Read-Along

### Audio model fields
`Audio`: `id, story (FK), title, slug, audio_file (mp3, ≤150MB), transcript (rich HTML), uploaded_at, order, duration_seconds (float, probed via mutagen), file_size_bytes, read_along_offset_ms (int, -5000..5000, default 0)`.

**`AudioSerializer`** (embedded in story detail / chapter action):
```json
{
  "id","title","slug","audio_file": "<url>", "order",
  "download_size_bytes": int,
  "has_transcript": bool,
  "read_along_available": bool,   // audio_file present AND transcript has real content
  "transcript_synchronized": bool // has_transcript AND timed cues exist
}
```

### AudioTranscriptCue model fields
`AudioTranscriptCue`: `id, audio (FK), order (int), start_ms (int), end_ms (int), text`. `end_ms` must be > `start_ms` (DB CheckConstraint). Unique per `(audio, order)`.

### Read-along — `GET /api/stories/{slug}/read-along/{audio_slug}/` (public) — THE key sync endpoint
```json
{
  "story": {
    "id","title","slug","language","story_type": "Novel",
    "cover_image": "https://.../900x1200/...",
    "author": {"id","name"} | null
  },
  "audio": {
    "id","title","slug","order",
    "audio_file": "<absolute url>|null",
    "stream_url": "<absolute url to byte-range stream endpoint>|null",
    "duration_seconds": float|null,
    "download_size_bytes": int,
    "has_transcript": bool,
    "read_along_available": bool,
    "transcript_synchronized": bool
  },
  "transcript": {
    "html": "<sanitized transcript html or empty string>",
    "state": "empty" | "synchronized" | "unsynchronized",
    "synchronized": bool,
    "cues": [
      { "id": int, "start_seconds": 12.345, "end_seconds": 15.0, "text": "spoken segment" },
      ...
    ],
    "default_offset_seconds": float   // read_along_offset_ms / 1000
  },
  "navigation": { "previous_audio_slug": "slug"|null, "next_audio_slug": "slug"|null }
}
```
Cue timing is **exposed in seconds** (converted from the model's millisecond fields), rounded to 3 decimals — this is exactly what a Flutter reader needs to highlight text against audio position. `previous_audio_slug`/`next_audio_slug` only consider tracks that have real transcript content (empty/blank markup is excluded).

### Video model fields
`Video`: `id, story (FK), title, slug, youtube_id (11-char), youtube_url, order, duration_seconds (float, optional), created_at`.

`VideoSerializer` (public, embedded in story detail): `{id,title,slug,youtube_id,order,duration_seconds,aspect_ratio}`. `aspect_ratio` is the stored video orientation (`16:9` or `9:16`); YouTube Shorts URLs are automatically marked `9:16`. The mobile client plays via YouTube using `youtube_id` (no server-hosted file).

---

## 5. Progress Tracking Endpoints

All require auth (`IsAuthenticated`) except analytics events. All progress-except-`reading-progress`'s top-level `progress` value use **monotonic max()** semantics server-side (a lower incoming value never decreases the stored one) — the response reflects the stored (post-max) value, not necessarily what was sent. `reading-progress`'s top-level `progress` is NOT maxed (always overwritten) but its `ChapterReadingProgress` breakdown IS maxed. GET 404s uniformly return `{"detail": "Progress not found."}`.

### `GET/PUT /api/reading-progress/{story_slug}/`
GET/PUT response (`ReadingProgressSerializer`):
```json
{
  "chapter_slug": "slug"|null,
  "progress": 0.0,                 // 0.0-1.0, position within current chapter
  "overall_progress": 0.0,         // avg of ChapterReadingProgress across all chapters, rounded 4dp
  "chapter_progresses": [ {"chapter_slug":, "progress":}, ... ],
  "last_element_id": "string"|null,
  "updated_at": "ISO datetime"
}
```
PUT body: `{ "chapter_slug": "slug" (optional), "progress": 0.0-1.0 (required), "last_element_id": "string" (optional) }`. On PUT, response additionally includes completion signals (see below).

**Completion signals** (`reading-progress`, `audio-progress`, `video-progress`, `file-reading-progress` PUT responses only — NOT on blog/quick-read progress):
```json
"story_completed": bool,        // true only on the write that finished the story
"unlocked_country": "..."|null, // set only if this finish unlocked a new country
"unlocked_achievements": [ ... ]  // achievements newly completed by this write
```

### `GET/PUT /api/audio-progress/{story_slug}/`
GET returns the most-recently-updated audio's progress. Response (`AudioReadingProgressSerializer`):
```json
{
  "audio_slug": "slug"|null, "progress": 0.0, "position_seconds": 0.0, "duration_seconds": 0.0,
  "overall_progress": 0.0,   // avg across all audios in the story
  "audio_progresses": [ {"audio_slug":,"progress":,"position_seconds":,"duration_seconds":}, ... ],
  "updated_at": "ISO datetime"
}
```
PUT body: `{ "audio_slug": "slug" (functionally required — 400 if missing/invalid), "progress": 0.0-1.0, "position_seconds": float (default 0), "duration_seconds": float (default 0) }`. `position_seconds`/`duration_seconds` are always overwritten (not maxed); `progress` is maxed.

### `GET/PUT /api/video-progress/{story_slug}/`
Identical structure, `video_slug`/`video_progresses` in place of audio equivalents.

### `GET/PUT /api/file-reading-progress/{story_slug}/{file_format}/`
`file_format` path segment must be `epub` or `pdf` (400 `{"detail":"Invalid format."}` otherwise).
```json
{ "format": "epub"|"pdf", "progress": 0.0, "position": "string"|null, "updated_at": "ISO datetime" }
```
PUT body: `{ "progress": 0.0-1.0 (required), "position": "string" (optional — EPUB CFI string, or PDF page number as text) }`. Includes completion signals on PUT.

### `GET/PUT /api/blog-reading-progress/{blog_slug}/`
```json
{ "progress": 0.0, "updated_at": "ISO datetime" }
```
PUT body: `{ "progress": 0.0-1.0 }`. No completion signals (blogs have no completion concept).

### `GET/PUT /api/quick-read-progress/{story_slug}/`
Same shape as blog progress. PUT returns 400 if the story has no non-blank `summary`. No completion signals.

### `POST /api/analytics/events/` — Public, throttled 120/min
Fire-and-forget analytics beacon. Bot/superuser traffic is silently accepted (`202`, empty body) without writing. Body (`AnalyticsEventWriteSerializer`):
```json
{
  "event_id": "uuid (required, client-generated, idempotent)",
  "event_type": "one of the enum below (required)",
  "visitor_id": "string ≤64 chars (required)",
  "session_id": "string ≤64 chars (optional)",
  "story_slug": "slug (optional — resolved server-side; silently null if not found)",
  "blog_slug": "slug (optional, same resolution)",
  "duration_seconds": "float 0-86400 (optional)",
  "value": "float (optional)",
  "metadata": "JSON object ≤2048 chars serialized (optional)"
}
```
`event_type` enum: `visit, ad_impression, reading_session, listening_session, watching_session, completion, download, read_along_cue_seek, read_along_follow_toggle, quick_read_opened, quick_read_completed, quick_read_full_story_clicked, story_started, story_resumed, story_progressed, story_completed, next_story_clicked, country_unlocked, passport_viewed, achievement_unlocked, journey_started, journey_completed, reaction_added, surprise_me_clicked, mood_selected, daily_story_viewed, daily_story_started, daily_story_completed`.

Response 201: `{ "event_id": "uuid", "unlocked_achievements": [...] }` (`unlocked_achievements` populated only for `quick_read_completed`, otherwise `[]`). Untracked (bot/superuser) requests get `202` with empty body.

### `POST /api/nepalikatha/events/` — Public companion-site analytics (Nepali site), throttled 120/min
Body: `event_id` (uuid), `event_type` (`visit`|`read` only), `visitor_id` (uuid), `session_id` (uuid), `path` (must start with `/`, no `//`, no `?`), `story_slug`/`chapter_slug` (optional, unicode slugs — `chapter_slug` is validation-only, never persisted), `duration_seconds` (int, 0-30 cap). Cross-field rules: `read` events require a real published+Nepali-flagged story/chapter and `duration_seconds > 0`; `visit` events must carry none of those. Always responds `202` empty body.

---

## 6. Library & Gamification

All under `/api/auth/` (mounted on `AuthenticationViewSet`), require auth unless noted.

### `GET /api/auth/library/continue-reading/`
Paginated (default 20). Items are unfinished stories only (`0 < overall_progress < 1`), ordered by most-recently-touched. `ContinueReadingItemSerializer`:
```json
{
  "story": StoryListSerializer,
  "chapter_slug":, "chapter_title":,
  "chapter_progress": 0.0, "overall_progress": 0.0,
  "updated_at": "ISO datetime",
  "excerpt": "text snippet at current progress",
  "remaining_minutes": int|null   // ceil'd estimate of time left, computed from cached reading-time
}
```

### `GET /api/auth/library/completed-reading/`
Same `ContinueReadingItemSerializer` shape, sourced from the durable `StoryCompletion` record (works for audiobooks/videos/EPUB/PDF too, not just chapter-based reading). `chapter_progress`/`overall_progress` are always 1.0; `excerpt` is always `""`.

### `GET /api/auth/library/continue-listening/`
`ContinueListeningItemSerializer`:
```json
{ "story": StoryListSerializer, "audio_slug":, "audio_title":, "audio_progress": 0.0, "overall_progress": 0.0, "updated_at":, "remaining_minutes": int|null }
```

### `GET /api/auth/library/continue-watching/`
`ContinueWatchingItemSerializer` — same shape with `video_slug`/`video_title`/`video_progress`.

### `GET /api/auth/library/reading-history/`
Everything ever opened (any surface), most-recently-touched first. `ReadingHistoryItemSerializer`:
```json
{ "story": StoryListSerializer, "last_read_at": "ISO datetime", "progress": 0.0, "completed": bool }
```

### `GET /api/auth/library/favorites/`
`FavoriteItemSerializer`: `{ "id":, "story": StoryListSerializer, "created_at": }`.

### `GET /api/auth/library/reviews/`
`MyReviewItemSerializer`: `{ "id":, "story": StoryListSerializer, "rating":, "comment":, "created_at":, "updated_at": }`.

### `GET /api/auth/library/recommendations/`
Query params: `quick_read=true` (only stories with a summary), `exclude=<slug>`. Response: array of `StoryListSerializer` (not paginated).

### `GET /api/auth/reading-streak/`
```json
{ "current_streak": int, "longest_streak": int, "unlocked_achievements": [...] }
```
Derived from `AnalyticsEvent` session rows (reading/listening/watching), not from progress tables.

### `GET /api/auth/achievements/`
```json
{
  "earned": int, "total": int,
  "results": [
    {
      "slug":, "name":, "description":, "category": "reading|countries|genre|streak|quick_read|journey",
      "icon": "emoji", "target_value": int,
      "progress": int, "completed": bool, "completed_at": "ISO datetime"|null
    }, ...
  ]
}
```
Hidden achievements (`hidden=True`) are omitted until earned.

### `GET /api/auth/story-passport/`
Reader's own country-completion summary — exact shape comes from `passport_summary(user)` in `apps/stats/passport.py` (not itemized here — read that file directly before implementing this screen).

### `GET /api/auth/story-passport/{country_code}/`
`country_code` = 2-letter ISO code.
```json
{
  "code":, "name":, "explored": bool,
  "stories_available": int, "stories_completed": int,
  "completed": [StoryListSerializer...],
  "continue_exploring": [StoryListSerializer...]
}
```
404 if `country_code` isn't a recognized country.

### `GET /api/auth/weekly-recap/`
"Your Week in Stories" — rolling 7 days:
```json
{
  "days": 7, "stories_completed": int, "minutes_read": int,
  "countries_explored": int, "journeys_completed": int,
  "current_streak": int, "favourite_genre": string|null,
  "has_activity": bool
}
```

### `GET /api/auth/profile-insights/`
Aggregated private reading-habit dashboard data:
```json
{
  "summary": {
    "titles_started": int, "titles_completed": int, "active_days_30": int,
    "favorite_genre": string|null, "total_reading_minutes": int, "countries_explored": int
  },
  "activity": [ {"date":"YYYY-MM-DD","reading":int,"listening":int,"watching":int}, ... ]  // last 14 days
  ,
  "formats": [ {"name":"Chapters","value":int}, {"name":"EPUB",...}, {"name":"PDF",...}, {"name":"Audio",...}, {"name":"Video",...} ],
  "genres": [ {"name":,"value":int}, ... up to 6 ]
}
```

---

## 7. Blog Endpoints

Already documented under §3 ("Story Content Endpoints → Blog"): `GET /api/blog/` (list, search/sort/link filters) and `GET /api/blog/{slug}/` (detail with `content` HTML, `linked_stories`, `linked_blogs`). Progress tracking for blog posts is `GET/PUT /api/blog-reading-progress/{blog_slug}/` (§5).

---

## 8. Error Response Format

No custom `EXCEPTION_HANDLER` is registered — this is stock DRF behavior throughout:

| Status | Shape |
|---|---|
| 400 Validation | `{"field_name": ["message"], ...}` (per-field) or `{"detail": "message"}` (non-field, or explicit `Response({"detail": ...}, status=400)` calls used throughout the custom views) |
| 401 Unauthorized | `{"detail": "Authentication credentials were not provided."}` or similar, plus `WWW-Authenticate` header |
| 403 Forbidden | `{"detail": "message"}` |
| 404 Not Found | `{"detail": "Not found."}` or a specific message like `{"detail": "Progress not found."}` |
| 429 Throttled | `{"detail": "Request was throttled. Expected available in N seconds."}` |
| 410 Gone | `{"message": "..."}` (used specifically by the disabled email/password auth actions — note: `message` key, not `detail`, in these specific hand-rolled responses) |
| 500 | No guaranteed JSON — DRF's handler returns `None` for unhandled exceptions, so it falls through to Django's own error handling. |

Note the inconsistency worth flagging to client authors: most hand-written `Response({...}, status=...)` calls in the custom views use `"detail"`, but the disabled-auth 410 responses use `"message"` instead — a Flutter client parsing errors should check both keys defensively.

---

## Reference: Story model taxonomy choices
- `LANGUAGE_CHOICES`: `en, es, fr, de, pt, it, hi, ne, ja, ko, zh, ar, ru`
- `COUNTRY_CHOICES`: full ISO 3166-1 alpha-2 list (~180 entries) — fetch dynamically via `/api/story-map/` or `/api/discover/` rather than hardcoding in the client.
- Mood `source` values: `admin` (always public) vs `ai` (public only once `reviewed=true`).
- `StoryReaction.reaction_type`: `loved, funny, surprising, emotional, thought_provoking`.
- `StoryJourney.type`: `curated, country, genre, theme`.
- `FileReadingProgress.format`: `epub, pdf`.

---

## Out of scope (not documented here)

Pure admin/CMS surfaces were intentionally excluded from this reference since a mobile client won't call them: all `admin/*` router endpoints, `api/admin/*` analytics views, `ckeditor5/`, `sitemap.xml`/`sitemap-np.xml`.

## Follow-ups before implementing certain screens

- `apps/stats/passport.py` — read directly for the exact `GET /api/auth/story-passport/` (root, no country code) response shape before building the Story Passport overview screen.
- `apps/stats/achievements.py` — read directly for the exact per-achievement object shape returned in `unlocked_achievements` arrays (progress-endpoint completion signals, reading-streak, analytics events) if byte-exact precision is needed.

**Files consulted (all under `/home/beebayk/projects/stories/worldstories_b`):** `core/urls.py`, `core/settings/base.py`, `core/libs/pagination.py`, `apps/story/api.py`, `apps/story/serializers.py`, `apps/story/filters.py`, `apps/story/models.py`, `apps/users/api.py`, `apps/users/serializers.py`, `apps/stats/views.py`, `apps/stats/serializers.py`, `apps/stats/models.py`, `apps/stats/nepalikatha.py`.
