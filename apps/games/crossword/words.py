from functools import lru_cache
from pathlib import Path
import re

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WORDLIST_PATH = DATA_DIR / "crossword_words.txt"
BLOCKLIST_PATH = DATA_DIR / "crossword_blocklist.txt"

_ALPHA_3_5_RE = re.compile(r"^[A-Z]{3,5}$")
_TOKEN_RE = re.compile(r"[A-Za-z]{3,5}")


@lru_cache(maxsize=1)
def get_blocklist() -> frozenset[str]:
    if not BLOCKLIST_PATH.exists():
        return frozenset()
    return frozenset(
        line.strip().upper()
        for line in BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


@lru_cache(maxsize=1)
def get_base_wordlist() -> frozenset[str]:
    blocklist = get_blocklist()
    if not WORDLIST_PATH.exists():
        return frozenset()
    words = set()
    for line in WORDLIST_PATH.read_text(encoding="utf-8").splitlines():
        w = line.strip().upper()
        if _ALPHA_3_5_RE.match(w) and w not in blocklist:
            words.add(w)
    return frozenset(words)


def clear_word_caches() -> None:
    get_blocklist.cache_clear()
    get_base_wordlist.cache_clear()


def get_allowed_words(include_db: bool = True) -> set[str]:
    blocklist = get_blocklist()
    words = set(get_base_wordlist())

    if include_db:
        from apps.games.models import CrosswordWord
        from apps.story.models import Author, Story

        for w in CrosswordWord.objects.values_list("word", flat=True):
            up = (w or "").strip().upper()
            if _ALPHA_3_5_RE.match(up):
                words.add(up)

        for title in Story.objects.published().values_list("title", flat=True):
            for tok in _TOKEN_RE.findall(title or ""):
                words.add(tok.upper())

        for name in Author.objects.values_list("name", flat=True):
            for tok in _TOKEN_RE.findall(name or ""):
                words.add(tok.upper())

    words.difference_update(blocklist)
    return words
