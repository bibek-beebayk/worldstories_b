from functools import lru_cache
import json
from pathlib import Path
import re

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WORDLIST_PATH = DATA_DIR / "crossword_words.txt"
BLOCKLIST_PATH = DATA_DIR / "crossword_blocklist.txt"
SCORES_PATH = DATA_DIR / "crossword_word_scores.json"
NAMES_PATH = DATA_DIR / "crossword_names.txt"

_ALPHA_3_5_RE = re.compile(r"^[A-Z]{3,5}$")
_TOKEN_RE = re.compile(r"[A-Za-z]{3,5}")

COMMON_ZIPF_THRESHOLD = 4.0
RARE_ZIPF_THRESHOLD = 3.5
SOLVER_MIN_ZIPF = 3.2
SOLVER_BANK_MIN_ZIPF = 2.5


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


@lru_cache(maxsize=1)
def get_word_scores() -> dict[str, float]:
    if not SCORES_PATH.exists():
        return {}
    raw = json.loads(SCORES_PATH.read_text(encoding="utf-8"))
    return {str(k).upper(): float(v) for k, v in raw.items()}


@lru_cache(maxsize=1)
def get_names_list() -> frozenset[str]:
    if not NAMES_PATH.exists():
        return frozenset()
    return frozenset(
        line.strip().upper()
        for line in NAMES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and _ALPHA_3_5_RE.match(line.strip().upper())
    )


@lru_cache(maxsize=1)
def get_author_name_tokens() -> frozenset[str]:
    try:
        from apps.story.models import Author

        tokens: set[str] = set()
        for name in (
            Author.objects.filter(stories__is_published=True)
            .distinct()
            .values_list("name", flat=True)
        ):
            for tok in _TOKEN_RE.findall(name or ""):
                tokens.add(tok.upper())
        return frozenset(tokens)
    except Exception:
        return frozenset()


def clear_word_caches() -> None:
    get_blocklist.cache_clear()
    get_base_wordlist.cache_clear()
    get_word_scores.cache_clear()
    get_names_list.cache_clear()
    get_author_name_tokens.cache_clear()


def zipf(word: str) -> float:
    if not word:
        return 0.0
    return float(get_word_scores().get(word.strip().upper(), 0.0))


def is_common(word: str) -> bool:
    return zipf(word) >= COMMON_ZIPF_THRESHOLD


def is_rare(word: str) -> bool:
    return zipf(word) < RARE_ZIPF_THRESHOLD


def is_name(word: str, clue_bank_flag: bool = False) -> bool:
    if clue_bank_flag:
        return True
    up = (word or "").strip().upper()
    if not up:
        return False
    if is_common(up):
        return False
    return up in get_names_list() or up in get_author_name_tokens()


def get_allowed_words(include_db: bool = True) -> set[str]:
    """Full allowed word set for manual validation (includes library vocabulary)."""
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


def get_solver_candidate_words(include_db: bool = True) -> tuple[set[str], set[str], set[str]]:
    """Candidate pool for auto-generation solver.

    Returns (pool_words, bank_non_name_words, flagged_name_words).
    - Base dictionary words with zipf >= 3.2
    - Plus ClueBank non-name entries with zipf >= 2.5
    - Excludes library vocabulary (story titles / author names) and blocklist.
    """
    blocklist = get_blocklist()
    scores = get_word_scores()

    pool = {
        w
        for w in get_base_wordlist()
        if scores.get(w, 0.0) >= SOLVER_MIN_ZIPF and w not in blocklist
    }

    bank_non_names: set[str] = set()
    flagged_names: set[str] = set()

    if include_db:
        from apps.games.models import ClueBank

        for ans, flag in ClueBank.objects.values_list("answer", "is_name"):
            up = (ans or "").strip().upper()
            if not _ALPHA_3_5_RE.match(up) or up in blocklist:
                continue
            if flag:
                flagged_names.add(up)
            else:
                bank_non_names.add(up)
                if scores.get(up, 0.0) >= SOLVER_BANK_MIN_ZIPF:
                    pool.add(up)

    pool.difference_update(blocklist)
    return pool, bank_non_names, flagged_names
