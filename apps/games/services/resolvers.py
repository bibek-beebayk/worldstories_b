from collections import Counter

from django.db.models import Count, Q

from apps.story.models import COUNTRY_CHOICES, Author, Story
from core.libs.images import get_cover_image_url

from .text import format_initials, html_to_plain_text, word_count

COUNTRY_NAME_MAP = dict(COUNTRY_CHOICES)


def decade_label(year: int | None) -> str | None:
    if year is None:
        return None
    decade = (int(year) // 10) * 10
    return f"{decade}s"


def compare_years(y1: int | None, y2: int | None) -> str:
    if y1 is None or y2 is None:
        return "different"
    if (int(y1) // 10) == (int(y2) // 10):
        return "same"
    if abs(int(y1) - int(y2)) <= 10:
        return "close"
    return "different"


def story_word_count(story: Story) -> int:
    total = 0
    chapters = story.chapters.all()
    for ch in chapters:
        total += word_count(html_to_plain_text(ch.content or ""))
    if total == 0 and getattr(story, "cached_chapter_reading_minutes", 0):
        total = int(story.cached_chapter_reading_minutes) * 220
    return total


def length_label_from_words(words: int) -> str | None:
    if words <= 0:
        return None
    if words < 12000:
        return "Short story"
    if words <= 40000:
        return "Novella"
    return "Novel"


def author_derived_facts(author: Author) -> dict:
    stories = list(
        Story.objects.published()
        .filter(author=author)
        .prefetch_related("genres")
        .order_by("-views", "title")
    )

    country_code = (author.country or "").strip()
    if not country_code:
        countries = [s.country for s in stories if s.country]
        if countries:
            counts = Counter(countries)
            # Highest count, deterministic tie-break by code
            country_code = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    year = author.birth_year
    if year is None:
        years = [s.original_published_year for s in stories if s.original_published_year is not None]
        if years:
            year = min(years)

    genre_map: dict[int, str] = {}
    for s in stories:
        for g in s.genres.all():
            genre_map[g.id] = g.name
    sorted_genres = sorted(genre_map.items(), key=lambda kv: kv[1].lower())

    works_count = len(stories)
    works_sample = stories[0].title if stories else None

    return {
        "stories": stories,
        "country_code": country_code,
        "country_name": COUNTRY_NAME_MAP.get(country_code) if country_code else None,
        "year": year,
        "era": decade_label(year),
        "genre_ids": {gid for gid, _ in sorted_genres},
        "genre_names": [gname for _, gname in sorted_genres],
        "works_count": works_count,
        "works_sample": works_sample,
    }


def author_years_display(author: Author) -> str:
    if author.years_label:
        return author.years_label
    if author.birth_year and author.death_year:
        return f"{author.birth_year}\u2013{author.death_year}"
    if author.birth_year:
        return f"b. {author.birth_year}"
    return ""


class StoryResolver:
    kind = "story"

    def search(self, q: str, limit: int = 8, request=None) -> list[dict]:
        query = (q or "").strip()
        if len(query) < 2:
            return []
        qs = (
            Story.objects.published()
            .select_related("author")
            .filter(Q(title__icontains=query) | Q(author__name__icontains=query))
            .order_by("-views", "title")[:limit]
        )
        results = []
        for story in qs:
            results.append(
                {
                    "id": story.id,
                    "title": story.title,
                    "author": story.author.name if story.author_id else "",
                    "cover_thumb": get_cover_image_url(
                        story.cover_image_file, story.cover_image, request, size="480x640"
                    ),
                }
            )
        return results

    def attributes(self, story: Story, request=None) -> dict:
        genres = list(story.genres.all().order_by("name"))
        genre_names = [g.name for g in genres]
        genre_ids = {g.id for g in genres}
        country_code = (story.country or "").strip()
        country_name = COUNTRY_NAME_MAP.get(country_code) if country_code else None
        year = story.original_published_year
        era = decade_label(year)
        words = story_word_count(story)
        length = length_label_from_words(words)
        initials = format_initials(story.author.name) if story.author_id else None
        cover = get_cover_image_url(
            story.cover_image_file, story.cover_image, request, size="480x640"
        )
        return {
            "genre": ", ".join(genre_names) if genre_names else None,
            "country": country_name,
            "era": era,
            "length": length,
            "initials": initials or None,
            "cover": cover or None,
            "_genre_ids": genre_ids,
            "_country_code": country_code,
            "_year": year,
        }

    def compare(self, answer: Story, guess: Story) -> dict:
        ans = self.attributes(answer)
        gue = self.attributes(guess)
        genre_match = bool(ans["_genre_ids"] and gue["_genre_ids"] and (ans["_genre_ids"] & gue["_genre_ids"]))
        country_match = bool(ans["_country_code"] and gue["_country_code"] and ans["_country_code"] == gue["_country_code"])
        era_match = compare_years(ans["_year"], gue["_year"])
        return {
            "correct": answer.id == guess.id,
            "matches": {
                "genre": genre_match,
                "country": country_match,
                "era": era_match,
            },
        }

    def card(self, answer: Story, request=None) -> dict:
        from apps.story.models import Audiobook

        audiobook = (
            Audiobook.objects.published()
            .filter(story=answer, track_count__gt=0)
            .only("slug")
            .first()
        )
        return {
            "kind": "story",
            "id": answer.id,
            "title": answer.title,
            "slug": answer.slug,
            "author": answer.author.name if answer.author_id else "",
            "year": answer.original_published_year,
            "cover": get_cover_image_url(
                answer.cover_image_file, answer.cover_image, request, size="480x640"
            ),
            "read_time": answer.cached_chapter_reading_minutes or None,
            "audiobook_slug": audiobook.slug if audiobook else None,
        }


class AuthorResolver:
    kind = "author"

    def search(self, q: str, limit: int = 8, request=None) -> list[dict]:
        query = (q or "").strip()
        if len(query) < 2:
            return []
        qs = (
            Author.objects.filter(name__icontains=query)
            .annotate(pub_count=Count("stories", filter=Q(stories__is_published=True)))
            .order_by("-pub_count", "name")[:limit]
        )
        results = []
        for author in qs:
            facts = author_derived_facts(author)
            parts = []
            if facts["country_name"]:
                parts.append(facts["country_name"])
            if facts["era"]:
                parts.append(facts["era"])
            results.append(
                {
                    "id": author.id,
                    "name": author.name,
                    "subtitle": ", ".join(parts),
                    "initials": format_initials(author.name),
                }
            )
        return results

    def attributes(self, author: Author, request=None) -> dict:
        facts = author_derived_facts(author)
        works_count = facts["works_count"]
        works_count_label = (
            f"{works_count} {'story' if works_count == 1 else 'stories'} on WorldStories"
            if works_count > 0
            else None
        )
        initials = format_initials(author.name)
        return {
            "genre": ", ".join(facts["genre_names"]) if facts["genre_names"] else None,
            "country": facts["country_name"],
            "era": facts["era"],
            "initials": initials or None,
            "works_count": works_count_label,
            "works_sample": facts["works_sample"],
            "cover": author.image or None,
            "_genre_ids": facts["genre_ids"],
            "_country_code": facts["country_code"],
            "_year": facts["year"],
        }

    def compare(self, answer: Author, guess: Author) -> dict:
        ans = self.attributes(answer)
        gue = self.attributes(guess)
        genre_match = bool(ans["_genre_ids"] and gue["_genre_ids"] and (ans["_genre_ids"] & gue["_genre_ids"]))
        country_match = bool(ans["_country_code"] and gue["_country_code"] and ans["_country_code"] == gue["_country_code"])
        era_match = compare_years(ans["_year"], gue["_year"])
        return {
            "correct": answer.id == guess.id,
            "matches": {
                "genre": genre_match,
                "country": country_match,
                "era": era_match,
            },
        }

    def card(self, answer: Author, request=None) -> dict:
        facts = author_derived_facts(answer)
        works = [
            {
                "id": s.id,
                "title": s.title,
                "slug": s.slug,
                "year": s.original_published_year,
                "cover": get_cover_image_url(
                    s.cover_image_file, s.cover_image, request, size="480x640"
                ),
            }
            for s in facts["stories"][:6]
        ]
        return {
            "kind": "author",
            "id": answer.id,
            "name": answer.name,
            "slug": answer.slug,
            "years": author_years_display(answer),
            "initials": format_initials(answer.name),
            "image": answer.image or None,
            "works": works,
        }


RESOLVERS = {
    "story": StoryResolver(),
    "author": AuthorResolver(),
}


def get_resolver(answer_kind: str):
    if answer_kind not in RESOLVERS:
        raise ValueError(f"Unsupported answer_kind: {answer_kind}")
    return RESOLVERS[answer_kind]
