from apps.story.models import Author, Story

from .text import (
    has_balanced_quotes,
    html_to_plain_text,
    split_sentences,
    starts_lowercase,
    word_count,
)
from .validation import forbidden_terms_for_story, leak_check_text


def _story_attribute_tail() -> list[dict]:
    return [
        {"kind": "attribute", "key": "genre", "label": "Genre"},
        {"kind": "attribute", "key": "country", "label": "Country"},
        {"kind": "attribute", "key": "era", "label": "Era"},
        {"kind": "attribute", "key": "length", "label": "Length"},
        {"kind": "attribute", "key": "initials", "label": "Author initials"},
    ]


class OpeningLineBuilder:
    puzzle_type = "opening_line"
    can_auto_build = True

    def candidates(self, story: Story, limit: int = 5) -> list[dict]:
        first_chapter = story.chapters.order_by("order", "id").first()
        if not first_chapter:
            return []
        plain = html_to_plain_text(first_chapter.content or "")
        sentences = split_sentences(plain)
        if not sentences:
            return []

        forbidden = forbidden_terms_for_story(story)
        preferred: list[dict] = []
        fallback: list[dict] = []
        for idx, sentence in enumerate(sentences):
            if leak_check_text(sentence, forbidden):
                continue
            wc = word_count(sentence)
            item = {
                "index": idx,
                "text": sentence,
                "word_count": wc,
                "chapter_id": first_chapter.id,
            }
            if 8 <= wc <= 40:
                preferred.append(item)
            elif wc >= 3:
                fallback.append(item)

        combined = preferred + fallback
        return combined[:limit]

    def default_clues(self, story: Story, chosen_text: str | None = None) -> list[dict]:
        line = (chosen_text or "").strip()
        if not line:
            cands = self.candidates(story, limit=1)
            line = cands[0]["text"] if cands else ""
        return [
            {"kind": "text", "label": "Opening line", "value": line},
            *_story_attribute_tail(),
        ]


class QuoteBuilder:
    puzzle_type = "quote"
    can_auto_build = True

    def candidates(self, story: Story, limit: int = 8) -> list[dict]:
        chapters = list(story.chapters.order_by("order", "id"))
        if not chapters:
            return []
        n = len(chapters)
        if n >= 3:
            start_idx = int(n * 0.3)
            end_idx = max(int(n * 0.8), start_idx + 1)
            selected_chapters = chapters[start_idx:end_idx]
        else:
            selected_chapters = chapters

        forbidden = forbidden_terms_for_story(story)
        scored: list[tuple[int, int, dict]] = []
        global_idx = 0
        for ch_pos, chapter in enumerate(selected_chapters):
            plain = html_to_plain_text(chapter.content or "")
            sentences = split_sentences(plain)
            for s_idx, sentence in enumerate(sentences):
                # Avoid picking the literal opening line if only 1-2 chapters exist
                if n < 3 and ch_pos == 0 and s_idx == 0:
                    continue
                wc = word_count(sentence)
                if not (10 <= wc <= 35):
                    continue
                if starts_lowercase(sentence):
                    continue
                if not has_balanced_quotes(sentence):
                    continue
                if leak_check_text(sentence, forbidden):
                    continue
                scored.append(
                    (
                        abs(wc - 20),
                        global_idx,
                        {
                            "index": global_idx,
                            "text": sentence,
                            "word_count": wc,
                            "chapter_id": chapter.id,
                        },
                    )
                )
                global_idx += 1

        scored.sort(key=lambda t: (t[0], t[1]))
        return [item for _, _, item in scored[:limit]]

    def default_clues(self, story: Story, chosen_text: str | None = None) -> list[dict]:
        line = (chosen_text or "").strip()
        if not line:
            cands = self.candidates(story, limit=1)
            line = cands[0]["text"] if cands else ""
        return [
            {"kind": "text", "label": "In-book quote", "value": line},
            *_story_attribute_tail(),
        ]


class TriviaBuilder:
    puzzle_type = "trivia"
    can_auto_build = False

    def candidates(self, story: Story, limit: int = 5) -> list[dict]:
        return []

    def default_clues(self, story: Story, chosen_text: str | None = None) -> list[dict]:
        return []

    def fallback_attribute_clues(self) -> list[dict]:
        return [
            {"kind": "attribute", "key": "genre", "label": "Genre"},
            {"kind": "attribute", "key": "country", "label": "Country"},
            {"kind": "attribute", "key": "era", "label": "Era"},
        ]


class AuthorBioBuilder:
    puzzle_type = "author_bio"
    can_auto_build = True

    def candidates(self, author: Author, limit: int = 5) -> list[dict]:
        return []

    def default_clues(self, author: Author, chosen_text: str | None = None) -> list[dict]:
        return [
            {"kind": "attribute", "key": "genre", "label": "Genres"},
            {"kind": "attribute", "key": "works_sample", "label": "Notable work"},
            {"kind": "attribute", "key": "country", "label": "Country"},
            {"kind": "attribute", "key": "era", "label": "Era"},
            {"kind": "attribute", "key": "works_count", "label": "Published works"},
            {"kind": "attribute", "key": "initials", "label": "Initials"},
        ]


BUILDERS = {
    "opening_line": OpeningLineBuilder(),
    "quote": QuoteBuilder(),
    "trivia": TriviaBuilder(),
    "author_bio": AuthorBioBuilder(),
}


def get_builder(puzzle_type: str):
    if puzzle_type not in BUILDERS:
        raise ValueError(f"Unsupported puzzle_type: {puzzle_type}")
    return BUILDERS[puzzle_type]
