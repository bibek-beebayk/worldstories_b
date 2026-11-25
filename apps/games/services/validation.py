from apps.games.models import DailyPuzzle
from apps.story.models import Author, Story

from .resolvers import get_resolver
from .text import normalize_for_leak

STOPWORDS = {
    "about", "after", "again", "against", "along", "also", "among", "before",
    "being", "between", "book", "could", "during", "from", "have", "into",
    "just", "more", "most", "only", "onto", "over", "part", "should", "some",
    "story", "such", "tale", "than", "that", "their", "them", "then", "there",
    "these", "they", "this", "those", "through", "under", "upon", "very",
    "were", "what", "when", "where", "which", "while", "whose", "will", "with",
    "within", "without", "would", "your",
}

VALID_STORY_ATTR_KEYS = {"genre", "country", "era", "length", "initials"}
VALID_AUTHOR_ATTR_KEYS = {"genre", "country", "era", "initials", "works_count", "works_sample"}


def forbidden_terms_for_story(story: Story) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def _add(raw: str):
        norm = normalize_for_leak(raw)
        if norm and norm not in seen:
            seen.add(norm)
            terms.append(norm)

    norm_title = normalize_for_leak(story.title)
    if norm_title:
        _add(norm_title)
        for token in norm_title.split():
            if len(token) >= 4 and token not in STOPWORDS:
                _add(token)

    if story.author_id:
        norm_author = normalize_for_leak(story.author.name)
        if norm_author:
            _add(norm_author)
            parts = norm_author.split()
            if parts and len(parts[-1]) >= 3:
                _add(parts[-1])
    return terms


def forbidden_terms_for_author(author: Author) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def _add(raw: str):
        norm = normalize_for_leak(raw)
        if norm and norm not in seen:
            seen.add(norm)
            terms.append(norm)

    norm_name = normalize_for_leak(author.name)
    if norm_name:
        _add(norm_name)
        for token in norm_name.split():
            if len(token) >= 3:
                _add(token)
    return terms


def leak_check_text(text: str, forbidden_terms: list[str]) -> list[str]:
    norm_text = normalize_for_leak(text)
    if not norm_text or not forbidden_terms:
        return []
    padded = f" {norm_text} "
    found: list[str] = []
    for term in forbidden_terms:
        if term and f" {term} " in padded:
            found.append(term)
    return found


def _extract_puzzle_parts(puzzle_like):
    if isinstance(puzzle_like, DailyPuzzle):
        puzzle_type = puzzle_like.puzzle_type
        answer_kind = puzzle_like.answer_kind
        answer = puzzle_like.answer_author if answer_kind == "author" else puzzle_like.answer_story
        clues = puzzle_like.clues or []
    else:
        puzzle_type = puzzle_like.get("puzzle_type", "")
        answer_kind = puzzle_like.get("answer_kind") or (
            "author" if puzzle_type == DailyPuzzle.PUZZLE_TYPE_AUTHOR_BIO else "story"
        )
        clues = puzzle_like.get("clues") or []
        answer = puzzle_like.get("answer")
        if answer is None:
            if answer_kind == "author":
                author_id = puzzle_like.get("answer_author_id") or puzzle_like.get("answer_id")
                answer = Author.objects.filter(pk=author_id).first() if author_id else None
            else:
                story_id = puzzle_like.get("answer_story_id") or puzzle_like.get("answer_id")
                answer = (
                    Story.objects.select_related("author")
                    .prefetch_related("genres", "chapters")
                    .filter(pk=story_id)
                    .first()
                    if story_id
                    else None
                )
    return puzzle_type, answer_kind, answer, clues


def leak_check(puzzle_like) -> list[dict]:
    _, answer_kind, answer, clues = _extract_puzzle_parts(puzzle_like)
    if answer is None:
        return []
    if answer_kind == "author":
        forbidden = forbidden_terms_for_author(answer)
    else:
        forbidden = forbidden_terms_for_story(answer)

    issues: list[dict] = []
    for idx, clue in enumerate(clues):
        if not isinstance(clue, dict) or clue.get("kind") != "text":
            continue
        value = clue.get("value") or ""
        matched = leak_check_text(value, forbidden)
        for term in matched:
            issues.append({"clue_index": idx, "term": term})
    return issues


def validate_for_approval(puzzle_like) -> list[str]:
    errors: list[str] = []
    puzzle_type, answer_kind, answer, clues = _extract_puzzle_parts(puzzle_like)

    valid_types = {choice[0] for choice in DailyPuzzle.PUZZLE_TYPE_CHOICES}
    if puzzle_type not in valid_types:
        errors.append(f"Invalid puzzle type: {puzzle_type or '(missing)'}.")
        return errors

    expected_kind = "author" if puzzle_type == DailyPuzzle.PUZZLE_TYPE_AUTHOR_BIO else "story"
    if answer_kind != expected_kind:
        errors.append(f"Puzzle type '{puzzle_type}' requires answer kind '{expected_kind}'.")

    if answer is None:
        errors.append(f"An answer ({expected_kind}) is required.")
        return errors

    if expected_kind == "author" and not isinstance(answer, Author):
        errors.append("Answer must be an Author for author_bio puzzles.")
        return errors
    if expected_kind == "story" and not isinstance(answer, Story):
        errors.append("Answer must be a Story for story puzzles.")
        return errors

    if not isinstance(clues, list) or not (4 <= len(clues) <= 6):
        errors.append("Puzzle must have between 4 and 6 clues.")

    resolver = get_resolver(expected_kind)
    attrs = resolver.attributes(answer)
    valid_attr_keys = VALID_AUTHOR_ATTR_KEYS if expected_kind == "author" else VALID_STORY_ATTR_KEYS

    text_clue_count = 0
    all_attribute_only = True
    for idx, clue in enumerate(clues if isinstance(clues, list) else []):
        pos = idx + 1
        if not isinstance(clue, dict):
            errors.append(f"Clue #{pos} must be an object.")
            all_attribute_only = False
            continue
        kind = clue.get("kind")
        label = (clue.get("label") or "").strip()
        if not label:
            errors.append(f"Clue #{pos} is missing a label.")
        if kind == "text":
            all_attribute_only = False
            val = (clue.get("value") or "").strip()
            if not val:
                errors.append(f"Clue #{pos} text cannot be empty.")
            else:
                text_clue_count += 1
        elif kind == "attribute":
            key = clue.get("key")
            if key not in valid_attr_keys:
                errors.append(f"Clue #{pos} has invalid attribute key '{key}'.")
            elif not attrs.get(key):
                errors.append(
                    f"Clue #{pos} ({label or key}) cannot be resolved from the selected {expected_kind}."
                )
        elif kind == "cover":
            all_attribute_only = False
            if not attrs.get("cover"):
                errors.append(f"Clue #{pos} (cover) has no cover image available.")
        else:
            all_attribute_only = False
            errors.append(f"Clue #{pos} has unknown kind '{kind}'.")

    if puzzle_type in (DailyPuzzle.PUZZLE_TYPE_OPENING_LINE, DailyPuzzle.PUZZLE_TYPE_QUOTE, DailyPuzzle.PUZZLE_TYPE_TRIVIA):
        if text_clue_count < 1:
            errors.append(f"{puzzle_type.replace('_', ' ').title()} puzzles require at least one text clue.")
    elif puzzle_type == DailyPuzzle.PUZZLE_TYPE_AUTHOR_BIO:
        if text_clue_count < 1 and not all_attribute_only:
            errors.append("Author bio puzzles require at least one text clue unless built purely from attributes.")

    leaks = leak_check(puzzle_like)
    for leak in leaks:
        errors.append(
            f"Clue #{leak['clue_index'] + 1} leaks the answer term '{leak['term']}'."
        )

    return errors
