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

from django.db.models import Count, Q

from apps.story.models import Favorite, Genre, Review, Story
from apps.story.serializers import similar_stories_candidates
from apps.stats.models import (
    AudioReadingProgress,
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
    )


def _genre_only_recommendations(genre_ids, exclude_q, limit):
    if not genre_ids:
        return Story.objects.none()
    return (
        Story.objects.published()
        .filter(genres__id__in=genre_ids)
        .exclude(exclude_q)
        .annotate(
            matching_genres=Count("genres", filter=Q(genres__id__in=genre_ids), distinct=True)
        )
        .select_related("author")
        .prefetch_related("genres", "audios")
        .distinct()
        .order_by("-matching_genres", "-rating", "-views", "-id")[:limit]
    )


def _blended_recommendations(user, preferred_genre_ids, engaged_story_ids, exclude_q, limit):
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

    candidates = (
        Story.objects.published()
        .filter(id__in=candidate_ids)
        .exclude(exclude_q)
        .annotate(
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


def recommend_stories_for(user, limit=RECOMMENDATION_LIMIT):
    preferred_genre_ids = set(user.preferred_genres.values_list("id", flat=True))
    engaged_story_ids = {
        story_id for _uid, story_id in _engagement_pairs(user_ids=[user.pk])
    }
    exclude_q = _already_engaged_q(user)

    if len(engaged_story_ids) < SUFFICIENT_DATA_THRESHOLD:
        return _genre_only_recommendations(preferred_genre_ids, exclude_q, limit)

    return _blended_recommendations(
        user, preferred_genre_ids, engaged_story_ids, exclude_q, limit
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
