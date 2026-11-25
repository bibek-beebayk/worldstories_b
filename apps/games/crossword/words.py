from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import string
import threading

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WORDLIST_PATH = DATA_DIR / "crossword_words.txt"
BLOCKLIST_PATH = DATA_DIR / "crossword_blocklist.txt"
SCORES_PATH = DATA_DIR / "crossword_word_scores.json"
NAMES_PATH = DATA_DIR / "crossword_names.txt"

_ALPHA_3_5_RE = re.compile(r"^[A-Z]{3,5}$")
_TOKEN_RE = re.compile(r"[A-Za-z]{3,5}")

COMMON_ZIPF_THRESHOLD = 4.0
RARE_ZIPF_THRESHOLD = 3.0
MISSING_WORD_ZIPF_DEFAULT = 3.0
SOLVER_MIN_ZIPF = 3.2
SOLVER_BANK_MIN_ZIPF = 2.5
DEFAULT_MIN_NON_NAME_WORDS = 30
SCARCE_LETTER_THRESHOLD = 0.02


@dataclass(frozen=True)
class BankWordIndex:
    answers: frozenset[str]
    answers_by_len: dict[int, tuple[str, ...]]
    non_name_words: frozenset[str]
    flagged_names: frozenset[str]
    min_times_used: dict[str, int]


_bank_cache_lock = threading.Lock()
_bank_cache_version: tuple[int, int, int, int] | None = None
_bank_cache_index: BankWordIndex | None = None


def clear_bank_word_cache() -> None:
    global _bank_cache_version, _bank_cache_index
    with _bank_cache_lock:
        _bank_cache_version = None
        _bank_cache_index = None


def get_bank_word_index() -> BankWordIndex:
    """Return cached bank-only candidate word index, invalidated on DB change or signal."""
    global _bank_cache_version, _bank_cache_index
    from django.db.models import Count, Max, Q, Sum
    from apps.games.models import ClueBank

    agg = ClueBank.objects.aggregate(
        c=Count("id"),
        m=Max("id"),
        n=Count("id", filter=Q(is_name=True)),
        u=Sum("times_used"),
    )
    version = (
        int(agg["c"] or 0),
        int(agg["m"] or 0),
        int(agg["n"] or 0),
        int(agg["u"] or 0),
    )

    with _bank_cache_lock:
        if _bank_cache_index is not None and _bank_cache_version == version:
            return _bank_cache_index

    blocklist = get_blocklist()
    min_used: dict[str, int] = {}
    flagged_names: set[str] = set()
    by_len_sets: dict[int, set[str]] = {3: set(), 4: set(), 5: set()}

    for ans, is_name_flag, times_used in ClueBank.objects.values_list(
        "answer", "is_name", "times_used"
    ):
        up = (ans or "").strip().upper()
        if not _ALPHA_3_5_RE.match(up) or up in blocklist:
            continue
        by_len_sets[len(up)].add(up)
        used = int(times_used or 0)
        if up not in min_used or used < min_used[up]:
            min_used[up] = used
        if is_name_flag:
            flagged_names.add(up)

    all_answers = frozenset().union(*by_len_sets.values())
    frozen_names = frozenset(flagged_names & all_answers)
    non_names = all_answers - frozen_names
    index = BankWordIndex(
        answers=all_answers,
        answers_by_len={k: tuple(sorted(v)) for k, v in by_len_sets.items()},
        non_name_words=non_names,
        flagged_names=frozen_names,
        min_times_used=min_used,
    )

    with _bank_cache_lock:
        _bank_cache_version = version
        _bank_cache_index = index
    return index


def compute_bank_health() -> dict:
    """Summarize ClueBank size, length distribution, multi-clue answers, and scarce letters."""
    from apps.games.models import ClueBank

    blocklist = get_blocklist()
    total_rows = ClueBank.objects.count()
    counts_by_answer: dict[str, int] = {}

    for ans in ClueBank.objects.values_list("answer", flat=True):
        up = (ans or "").strip().upper()
        if not _ALPHA_3_5_RE.match(up) or up in blocklist:
            continue
        counts_by_answer[up] = counts_by_answer.get(up, 0) + 1

    distinct_answers = list(counts_by_answer.keys())
    total_answers = len(distinct_answers)
    by_length = {
        "3": sum(1 for w in distinct_answers if len(w) == 3),
        "4": sum(1 for w in distinct_answers if len(w) == 4),
        "5": sum(1 for w in distinct_answers if len(w) == 5),
    }
    multi_clue_answers = sum(1 for cnt in counts_by_answer.values() if cnt >= 2)

    scarce_letters: list[dict] = []
    if total_answers > 0:
        letter_counts = {ch: 0 for ch in string.ascii_uppercase}
        for w in distinct_answers:
            for ch in set(w):
                if ch in letter_counts:
                    letter_counts[ch] += 1
        scarce = []
        for ch, cnt in letter_counts.items():
            ratio = cnt / total_answers
            if ratio < SCARCE_LETTER_THRESHOLD:
                scarce.append(
                    {
                        "letter": ch,
                        "count": cnt,
                        "percent": round(ratio * 100, 2),
                    }
                )
        scarce.sort(key=lambda item: (item["count"], item["letter"]))
        scarce_letters = scarce[:5]

    # Pick weakest length (prefer recommending 5, then 4, then 3 on ties)
    weakest_len = min(
        ("5", "4", "3"),
        key=lambda length_key: (by_length[length_key], -int(length_key)),
    )
    if scarce_letters:
        letters_str = ", ".join(item["letter"] for item in scarce_letters)
        suggestion = (
            f"Add more clues, especially {weakest_len}-letter answers "
            f"(scarce letters: {letters_str})."
        )
    else:
        suggestion = f"Add more clues, especially {weakest_len}-letter answers."

    return {
        "total": total_rows,
        "total_answers": total_answers,
        "by_length": by_length,
        "multi_clue_answers": multi_clue_answers,
        "scarce_letters": scarce_letters,
        "suggestion": suggestion,
    }


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
    clear_bank_word_cache()


def zipf(word: str, is_name_word: bool | None = None) -> float:
    if not word:
        return 0.0
    up = word.strip().upper()
    if not up:
        return 0.0
    scores = get_word_scores()
    if up in scores:
        return float(scores[up])
    if is_name_word is True or (is_name_word is None and up in get_names_list()):
        return 0.0
    return MISSING_WORD_ZIPF_DEFAULT


def is_common(word: str) -> bool:
    return zipf(word) >= COMMON_ZIPF_THRESHOLD


def is_rare(word: str, clue_bank_flag: bool = False) -> bool:
    if not word:
        return False
    if is_name(word, clue_bank_flag=clue_bank_flag):
        return False
    return zipf(word, is_name_word=False) < RARE_ZIPF_THRESHOLD


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
    """Bank-only candidate pool for auto-generation solver.

    Returns (pool_words, bank_non_name_words, flagged_name_words).
    Never falls back to the dictionary word list.
    """
    if not include_db:
        return set(), set(), set()

    idx = get_bank_word_index()
    return set(idx.answers), set(idx.non_name_words), set(idx.flagged_names)
