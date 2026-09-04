"""Recommendation engine behind the homepage "Recommended for You" section.

Two paths, chosen per-user based on how much signal we have:

  - Cold start (fewer than SUFFICIENT_DATA_THRESHOLD engaged stories): rank
    by the genres the user explicitly picked at onboarding/in settings.
    That's all we have to go on yet.

  - Warm (enough engagement history): blend three signals — the explicit
    preferred_genres, an *implicit* genre affinity derived from what the
    user has actually engaged with (which may have drifted from what they
    originally picked), and a collaborative signal from what other users
    with overlapping engagement have read that this user hasn't.

Deliberately implemented as plain Python over a handful of small, targeted
queries rather than one large annotated ORM queryset. Combining multiple
Count() aggregates over different to-many relations in a single query is a
well-known Django footgun — the joins fan out and inflate unrelated counts —
and at this platform's current scale, the extra round trips here cost
nothing next to the correctness risk of getting that one big query subtly
wrong.
"""

from collections import Counter

from django.db.models import Count, F, Q
from django.db.models.functions import Abs, Coalesce

from apps.story.models import Favorite, Genre, Review, Story
from apps.story.serializers import similar_stories_candidates
from apps.stats.models import (
    AudioReadingProgress,
    VideoWatchProgress,
    ChapterReadingProgress,
    FileReadingProgress,
    ReadingProgress,
)

# A story counts as "engaged with" once progress crosses this fraction —
# a couple of pages in shouldn't carry the same weight as actually reading
# most of it.
ENGAGEMENT_PROGRESS_THRESHOLD = 0.5

# Below this many distinct engaged stories, there isn't enough signal for
# the implicit-genre/collaborative path to be meaningful — fall back to the
# explicit genre picks alone.
SUFFICIENT_DATA_THRESHOLD = 3

# How many of the most-overlapping other users to treat as "similar" when
# building the collaborative signal. Bounds the size of the second query
# rather than pulling in every user who ever touched a shared story.
MAX_SIMILAR_USERS = 20

RECOMMENDATION_LIMIT = 12
BECAUSE_FINISHED_LIMIT = 8

# Heuristic weights for blending the two warm-path signals — a matching
# genre counts for more than one similar user having read something, but
# both move the ranking. Tune freely; nothing downstream depends on the
# exact values.
GENRE_MATCH_WEIGHT = 2
COLLABORATIVE_WEIGHT = 1


def _engagement_pairs(user_ids=None, story_ids=None):
    """(user_id, story_id) pairs across every "this user cared about this
    story" signal: favorited, reviewed, or read/listened past the
    engagement threshold. Optionally scoped to a set of users and/or
    stories to keep each query small.
    """
    querysets = [
        Favorite.objects.values_list("user_id", "story_id"),
        Review.objects.values_list("user_id", "story_id"),
        ReadingProgress.objects.filter(
            progress__gte=ENGAGEMENT_PROGRESS_THRESHOLD
        ).values_list("user_id", "story_id"),
        ChapterReadingProgress.objects.filter(
            progress__gte=ENGAGEMENT_PROGRESS_THRESHOLD
        ).values_list("user_id", "story_id"),
        FileReadingProgress.objects.filter(
            progress__gte=ENGAGEMENT_PROGRESS_THRESHOLD
        ).values_list("user_id", "story_id"),
        AudioReadingProgress.objects.filter(
            progress__gte=ENGAGEMENT_PROGRESS_THRESHOLD
        ).values_list("user_id", "story_id"),
        VideoWatchProgress.objects.filter(
            progress__gte=ENGAGEMENT_PROGRESS_THRESHOLD
        ).values_list("user_id", "story_id"),
    ]
    if user_ids is not None:
        querysets = [qs.filter(user_id__in=user_ids) for qs in querysets]
    if story_ids is not None:
        querysets = [qs.filter(story_id__in=story_ids) for qs in querysets]

    pairs = set()
    for queryset in querysets:
        pairs.update(queryset)
    return pairs


def _already_engaged_q(user):
    return (
        Q(favorites__user=user)
        | Q(reviews__user=user)
        | Q(reading_progress__user=user)
        | Q(chapter_reading_progress__user=user)
        | Q(file_reading_progress__user=user)
        | Q(audio_reading_progress__user=user)
        | Q(video_watch_progress__user=user)
    )


def _apply_optional_filters(queryset, require_summary, exclude_story_id):
    if require_summary:
        queryset = queryset.exclude(Q(summary__isnull=True) | Q(summary__exact=""))
    if exclude_story_id is not None:
        queryset = queryset.exclude(id=exclude_story_id)
    return queryset


def _genre_only_recommendations(genre_ids, exclude_q, limit, require_summary=False, exclude_story_id=None):
    if not genre_ids:
        return Story.objects.none()
    candidates = Story.objects.published().filter(genres__id__in=genre_ids).exclude(exclude_q)
    candidates = _apply_optional_filters(candidates, require_summary, exclude_story_id)
    return (
        candidates.annotate(
            matching_genres=Count("genres", filter=Q(genres__id__in=genre_ids), distinct=True)
        )
        .select_related("author")
        .prefetch_related("genres", "audios")
        .distinct()
        .order_by("-matching_genres", "-rating", "-views", "-id")[:limit]
    )


def _blended_recommendations(
    user, preferred_genre_ids, engaged_story_ids, exclude_q, limit, require_summary=False, exclude_story_id=None
):
    implicit_genre_ids = set(
        Genre.objects.filter(stories__id__in=engaged_story_ids).values_list("id", flat=True)
    )
    genre_affinity_ids = preferred_genre_ids | implicit_genre_ids

    # Who else engaged with the same stories, ranked by how much overlap —
    # that's our stand-in for "similar interests".
    neighbor_pairs = _engagement_pairs(story_ids=engaged_story_ids)
    overlap_counts = Counter(uid for uid, _sid in neighbor_pairs if uid != user.pk)
    similar_user_ids = [uid for uid, _count in overlap_counts.most_common(MAX_SIMILAR_USERS)]

    # What those similar users engaged with that this user hasn't yet.
    their_pairs = _engagement_pairs(user_ids=similar_user_ids)
    collaborative_scores = Counter(
        story_id for _uid, story_id in their_pairs if story_id not in engaged_story_ids
    )

    genre_candidate_ids = (
        set(
            Story.objects.published()
            .filter(genres__id__in=genre_affinity_ids)
            .exclude(exclude_q)
            .values_list("id", flat=True)
        )
        if genre_affinity_ids
        else set()
    )

    candidate_ids = genre_candidate_ids | set(collaborative_scores.keys())
    if not candidate_ids:
        return Story.objects.none()

    candidates = Story.objects.published().filter(id__in=candidate_ids).exclude(exclude_q)
    candidates = _apply_optional_filters(candidates, require_summary, exclude_story_id)
    candidates = (
        candidates.annotate(
            matching_genres=Count(
                "genres", filter=Q(genres__id__in=genre_affinity_ids), distinct=True
            )
        )
        .select_related("author")
        .prefetch_related("genres", "audios")
        .distinct()
    )

    def score(story):
        return (
            story.matching_genres * GENRE_MATCH_WEIGHT
            + collaborative_scores.get(story.id, 0) * COLLABORATIVE_WEIGHT,
            story.rating,
            story.views,
            story.id,
        )

    return sorted(candidates, key=score, reverse=True)[:limit]


def recommend_stories_for(user, limit=RECOMMENDATION_LIMIT, require_summary=False, exclude_story_id=None):
    """`require_summary` restricts candidates to stories with a summary
    (i.e. Quick-Read-eligible) — used for the personalized "Recommended
    Quick Reads" list on the Quick Read page. `exclude_story_id` additionally
    excludes one specific story (the one currently being viewed) from its
    own recommendations. Both default to off, so the homepage's existing
    "Recommended for You" call is unaffected."""
    preferred_genre_ids = set(user.preferred_genres.values_list("id", flat=True))
    engaged_story_ids = {
        story_id for _uid, story_id in _engagement_pairs(user_ids=[user.pk])
    }
    exclude_q = _already_engaged_q(user)

    if len(engaged_story_ids) < SUFFICIENT_DATA_THRESHOLD:
        return _genre_only_recommendations(
            preferred_genre_ids, exclude_q, limit, require_summary, exclude_story_id
        )

    return _blended_recommendations(
        user, preferred_genre_ids, engaged_story_ids, exclude_q, limit, require_summary, exclude_story_id
    )


def recommend_because_finished(user, story, limit=BECAUSE_FINISHED_LIMIT):
    """"Because you finished X" — get_similar_stories's candidate pool for
    `story`, re-ranked toward this user's own taste (preferred_genres +
    genres of what they've engaged with) and excluding anything they've
    already engaged with. Always primarily ordered by shared_genres (match
    with X itself) so the rail stays recognizably anchored to X; the user's
    own taste only breaks ties among similarly-matched candidates, unlike
    recommend_stories_for's collaborative signal, which has no guaranteed
    relationship to X and would risk drowning out the anchor."""
    candidates, _ = similar_stories_candidates(story)
    candidates = candidates.exclude(_already_engaged_q(user))

    preferred_genre_ids = set(user.preferred_genres.values_list("id", flat=True))
    engaged_story_ids = {
        story_id for _uid, story_id in _engagement_pairs(user_ids=[user.pk])
    }
    implicit_genre_ids = set(
        Genre.objects.filter(stories__id__in=engaged_story_ids).values_list("id", flat=True)
    )
    user_genre_ids = preferred_genre_ids | implicit_genre_ids

    return candidates.annotate(
        user_genre_match=Count(
            "genres", filter=Q(genres__id__in=user_genre_ids), distinct=True
        ),
    ).order_by(
        "-shared_genres",
        "-user_genre_match",
        "-same_author",
        "-same_story_type",
        "-same_language",
        "-rating",
        "-views",
        "-id",
    )[:limit]


# How far a story's estimate may sit from the one just finished and still count
# as "about the same length". Proportional rather than a fixed number of
# minutes: five minutes either side is a different experience for a ten-minute
# folk tale and no difference at all for a two-hour novella.
SIMILAR_LENGTH_TOLERANCE = 0.4
COMPLETION_SECTION_LIMIT = 6


def _story_minutes_annotation():
    """The story's reading estimate as something the database can compare.

    Reads the denormalized columns rather than the live calculation — the live
    one word-counts every chapter body, which is fine for one story and
    impossible to filter a catalogue by. See reading_time.story_reading_minutes_cached.
    """
    return Coalesce("cached_chapter_reading_minutes", "cached_file_reading_minutes")


# How the single "read next" pick is chosen. Deliberately a readable table of
# weights rather than a hidden model: an editor should be able to look at this
# and predict what the site will suggest, and the requirements document (§12.3)
# asks for exactly this shape.
#
# The two "already met this story" penalties dwarf everything else because §2.2
# ranks *unread* above every other consideration — but they are penalties, not
# filters, so a reader who has genuinely finished everything similar still gets
# a suggestion instead of an empty panel ("avoid already completed stories
# unless no alternatives exist").
PRIMARY_WEIGHTS = {
    "already_completed": -100,
    "already_started": -25,
    "shared_genre": 3,
    "shared_category": 2,
    "same_country": 3,
    "similar_length": 2,
    "matches_reader_taste": 3,
    "same_author": 1,
    "same_story_type": 1,
    "same_language": 1,
}

# Scored in Python over a bounded slice of the candidate pool rather than as one
# large annotated query — same reasoning as this module's header: combining
# several Count aggregates over different to-many relations fans the joins out
# and inflates unrelated counts. Sixty is far more than enough to find a good
# primary from an already similarity-ordered pool.
PRIMARY_CANDIDATE_POOL = 60


def _score_primary_candidate(candidate, context):
    """Score one candidate for the "read next" slot. Returns (score, reasons).

    `reasons` is not used by the API yet; it exists so the choice can be
    explained — to an editor debugging a bad suggestion, or in the UI later —
    without re-deriving the arithmetic.
    """
    weights = PRIMARY_WEIGHTS
    score = 0
    reasons = []

    def add(key, condition, multiplier=1):
        nonlocal score
        if condition:
            score += weights[key] * multiplier
            reasons.append(key)

    add("already_completed", candidate.id in context["completed_ids"])
    add(
        "already_started",
        candidate.id in context["engaged_ids"] and candidate.id not in context["completed_ids"],
    )

    shared_genres = len(context["genre_ids"] & context["candidate_genres"].get(candidate.id, set()))
    add("shared_genre", shared_genres, multiplier=min(shared_genres, 3))

    shared_categories = len(
        context["category_ids"] & context["candidate_categories"].get(candidate.id, set())
    )
    add("shared_category", shared_categories, multiplier=min(shared_categories, 2))

    add("same_country", bool(context["country"]) and candidate.country == context["country"])

    # "A reasonable reading length" reads, in context, as one close to the
    # story just finished — a reader who has just enjoyed a ten-minute folk
    # tale is more likely to want another than a two-hour novella.
    minutes = context["minutes"]
    candidate_minutes = (
        candidate.cached_chapter_reading_minutes or candidate.cached_file_reading_minutes
    )
    if minutes and candidate_minutes:
        window = max(1, round(minutes * SIMILAR_LENGTH_TOLERANCE))
        add("similar_length", abs(candidate_minutes - minutes) <= window)

    taste_match = len(
        context["reader_genre_ids"] & context["candidate_genres"].get(candidate.id, set())
    )
    add("matches_reader_taste", taste_match, multiplier=min(taste_match, 2))

    add("same_author", context["author_id"] and candidate.author_id == context["author_id"])
    add("same_story_type", candidate.story_type_id == context["story_type_id"])
    add("same_language", candidate.language == context["language"])

    return score, reasons


def select_primary_recommendation(user, story):
    """The single story to put behind the "Read Next" button.

    Applies the preference order the requirements document sets out in §2.2 —
    unread first, then genre/category similarity, reading length, country, and
    the reader's own taste — over the same candidate pool the rest of the
    completion screen uses. Hard exclusions are only the ones that would be
    plainly wrong: the story just finished, and anything in its translation
    group (the same tale in another language).

    Returns None only when the pool is genuinely empty.
    """
    is_authenticated = bool(user and user.is_authenticated)

    candidates, _ = similar_stories_candidates(story)
    candidates = list(
        candidates.order_by(
            "-shared_genres", "-same_author", "-same_story_type",
            "-same_language", "-rating", "-views", "-id",
        )[:PRIMARY_CANDIDATE_POOL]
    )
    if not candidates:
        return None

    candidate_ids = [candidate.id for candidate in candidates]

    def related_ids(model_field):
        grouped = {}
        for story_id, related_id in Story.objects.filter(id__in=candidate_ids).values_list(
            "id", f"{model_field}__id"
        ):
            if related_id is not None:
                grouped.setdefault(story_id, set()).add(related_id)
        return grouped

    completed_ids = set()
    engaged_ids = set()
    reader_genre_ids = set()
    if is_authenticated:
        from apps.stats.models import StoryCompletion

        completed_ids = set(
            StoryCompletion.objects.filter(user=user, story_id__in=candidate_ids).values_list(
                "story_id", flat=True
            )
        )
        engaged_ids = {
            story_id
            for _uid, story_id in _engagement_pairs(user_ids=[user.pk], story_ids=candidate_ids)
        }
        engaged_story_ids = {
            story_id for _uid, story_id in _engagement_pairs(user_ids=[user.pk])
        }
        reader_genre_ids = set(user.preferred_genres.values_list("id", flat=True)) | set(
            Genre.objects.filter(stories__id__in=engaged_story_ids).values_list("id", flat=True)
        )

    context = {
        "completed_ids": completed_ids,
        "engaged_ids": engaged_ids,
        "reader_genre_ids": reader_genre_ids,
        "genre_ids": set(story.genres.values_list("id", flat=True)),
        "category_ids": set(story.categories.values_list("id", flat=True)),
        "candidate_genres": related_ids("genres"),
        "candidate_categories": related_ids("categories"),
        "country": story.country or None,
        "minutes": story.cached_chapter_reading_minutes or story.cached_file_reading_minutes,
        "author_id": story.author_id,
        "story_type_id": story.story_type_id,
        "language": story.language,
    }

    # max() keeps the first of any tie, and the pool arrives in the existing
    # similarity order — so ties fall back to that ordering rather than to
    # whatever the database happened to return.
    return max(
        candidates,
        key=lambda candidate: _score_primary_candidate(candidate, context)[0],
    )


def completion_recommendations(user, story, limit=COMPLETION_SECTION_LIMIT):
    """What to offer a reader at the moment they finish `story`.

    One primary pick plus three themed sections. Everything is drawn from the
    existing recommendation path — `recommend_because_finished` for the
    personalized ranking, `similar_stories_candidates` for the anonymous
    fallback — rather than a second scoring system: the requirements document
    is explicit that the end of a story should reuse the "Because you finished"
    logic, and two rankings that disagree about the same reader would be worse
    than one.

    Returns `(primary, sections)`. `primary` may be None — a brand-new
    catalogue, or a reader who has genuinely engaged with everything similar —
    and each section may be empty. Callers render what is there rather than
    padding with filler.
    """
    is_authenticated = bool(user and user.is_authenticated)

    # The primary pick has its own preference order (§2.2) and its own
    # fallback behaviour, so it is chosen separately rather than taken off the
    # top of the rail — see select_primary_recommendation.
    primary = select_primary_recommendation(user, story)

    if is_authenticated:
        pool = list(recommend_because_finished(user, story, limit=limit + 1))
    else:
        # No taste signal to rank by, so the generic similarity order stands.
        candidates, _ = similar_stories_candidates(story)
        pool = list(
            candidates.order_by(
                "-shared_genres", "-same_author", "-same_story_type",
                "-same_language", "-rating", "-views", "-id",
            )[: limit + 1]
        )

    more_like_this = [
        candidate for candidate in pool if primary is None or candidate.id != primary.id
    ][:limit]

    # Never the story just finished, and never anything from its translation
    # group — offering the same tale in another language reads as a bug.
    exclude = Q(translation_group=story.translation_group)
    if is_authenticated:
        exclude |= _already_engaged_q(user)
    if primary is not None:
        exclude |= Q(id=primary.id)

    base = Story.objects.published().for_card_list().exclude(exclude).distinct()

    more_from_country = []
    if story.country:
        more_from_country = list(
            base.filter(country=story.country).order_by("-rating", "-views", "-id")[:limit]
        )

    similar_length = []
    minutes = story.cached_chapter_reading_minutes or story.cached_file_reading_minutes
    if minutes:
        window = max(1, round(minutes * SIMILAR_LENGTH_TOLERANCE))
        similar_length = list(
            base.annotate(story_minutes=_story_minutes_annotation())
            .filter(
                story_minutes__isnull=False,
                story_minutes__gte=minutes - window,
                story_minutes__lte=minutes + window,
            )
            # Closest in length first — that is the whole point of the section.
            .annotate(length_gap=Abs(F("story_minutes") - minutes))
            .order_by("length_gap", "-rating", "-views", "-id")[:limit]
        )

    sections = [
        ("more_like_this", "More like this story", more_like_this),
        ("more_from_country", "More from this country", more_from_country),
        ("similar_length", "Similar reading length", similar_length),
    ]
    return primary, sections
