"""The Story Passport: where a reader has been, via the stories they finished.

Deliberately **derived, not stored.** The requirements document (§5.3) asks to
prefer derivation and to persist a `UserCountryProgress` only if timestamps,
first-unlock events or performance actually demand it. Measured against
`StoryCompletion`, none of them do:

  * per-country counts are one grouped query over the reader's completions;
  * `unlocked_at` is the earliest `completed_at` among a country's completions,
    available from the same aggregate;
  * a first unlock is "this country's completion count is exactly one", checked
    once at completion time — see `newly_unlocked_country`.

A second table would have to be kept in step with `StoryCompletion` forever,
and every drift between them would be a reader's passport quietly disagreeing
with their completed list. That is the trade this module declines to make.

Distinct from the Story Map (`StoryMapAPIView`), which is the *catalogue*
grouped by country and identical for everyone. The Passport is one reader's
own progress laid over it.
"""

from django.db.models import Count, Min

from apps.story.models import COUNTRY_CHOICES, Story

COUNTRY_NAMES = dict(COUNTRY_CHOICES)


def available_countries():
    """`{code: published story count}` for countries that have any story.

    The denominator on the Passport page. Counted from published stories rather
    than from COUNTRY_CHOICES, which has 196 entries — telling a reader they
    have explored 12 of 196 countries would be measuring them against a
    catalogue that does not exist.
    """
    return dict(
        Story.objects.published()
        .exclude(country="")
        .values_list("country")
        .annotate(total=Count("id"))
    )


def explored_countries(user):
    """`{code: {"completed": n, "unlocked_at": datetime}}` for this reader.

    A country is explored once the reader completes at least one full story
    from it (§5.2). Quick Read cannot unlock a country: summaries never create
    a `StoryCompletion` — see apps/stats/completion.py.
    """
    from apps.stats.models import StoryCompletion

    rows = (
        StoryCompletion.objects.filter(user=user)
        .exclude(story__country="")
        .values("story__country")
        .annotate(completed=Count("id"), unlocked_at=Min("completed_at"))
    )
    return {
        row["story__country"]: {
            "completed": row["completed"],
            "unlocked_at": row["unlocked_at"],
        }
        for row in rows
    }


def passport_summary(user):
    """Everything the Passport page needs, in two queries."""
    available = available_countries()
    explored = explored_countries(user) if user and user.is_authenticated else {}

    countries = [
        {
            "code": code,
            "name": COUNTRY_NAMES.get(code, code),
            "stories_available": total,
            "stories_completed": explored.get(code, {}).get("completed", 0),
            "explored": code in explored,
            "unlocked_at": explored.get(code, {}).get("unlocked_at"),
        }
        for code, total in available.items()
    ]
    # Explored first, then by how much there is left to read — the page is a
    # record of where you have been and an invitation to go somewhere else.
    countries.sort(key=lambda row: (not row["explored"], -row["stories_available"], row["name"]))

    return {
        "countries_explored": sum(1 for row in countries if row["explored"]),
        "countries_available": len(available),
        "stories_completed": sum(row["stories_completed"] for row in countries),
        "countries": countries,
    }


def newly_unlocked_country(user, story):
    """The country code this completion just unlocked, or None.

    Called immediately after a `StoryCompletion` is created. "First" is the
    country having exactly one completion — which is only true on the call that
    created it, so this cannot fire twice for the same country however many
    devices the reader finishes stories on.
    """
    from apps.stats.models import StoryCompletion

    if not story.country:
        return None

    completions_from_country = StoryCompletion.objects.filter(
        user=user, story__country=story.country
    ).count()
    return story.country if completions_from_country == 1 else None
