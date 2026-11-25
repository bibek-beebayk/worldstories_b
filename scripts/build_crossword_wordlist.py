"""Dev-only script to generate apps/games/data/crossword_words.txt and
apps/games/data/crossword_word_scores.json using wordfreq.

Usage (dev environment only; do NOT add wordfreq to production requirements):
    pip install wordfreq
    python scripts/build_crossword_wordlist.py
"""

import json
import os
from pathlib import Path
import re
import sys

from wordfreq import top_n_list, zipf_frequency

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "apps" / "games" / "data"
BLOCKLIST_PATH = DATA_DIR / "crossword_blocklist.txt"
OUTPUT_WORDS_PATH = DATA_DIR / "crossword_words.txt"
OUTPUT_SCORES_PATH = DATA_DIR / "crossword_word_scores.json"

ASCII_ALPHA_RE = re.compile(r"^[a-z]{3,5}$")
DEFAULT_MISSING_SCORE = 3.0

REQUIRED_WORDS = [
    "SPAM",
    "TALES",
    "AGILE",
    "RAVEN",
    "NEED",
    "STAR",
    "PAGAN",
    "ALIVE",
    "MELEE",
    "SEND",
]


def load_blocklist() -> set[str]:
    if not BLOCKLIST_PATH.exists():
        return set()
    return {
        line.strip().upper()
        for line in BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def load_clue_bank_answers() -> set[str]:
    """Load distinct 3-5 letter alphabetic ClueBank answers via Django ORM and data/clues*.csv."""
    answers: set[str] = set()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
        import django

        django.setup()
        from apps.games.models import ClueBank

        for ans in ClueBank.objects.values_list("answer", flat=True):
            cleaned = (ans or "").strip().lower()
            if ASCII_ALPHA_RE.match(cleaned):
                answers.add(cleaned.upper())
    except Exception as exc:
        print(f"Note: could not load ClueBank from DB ({exc}); scoring wordlist + CSVs.")

    # Also include any answers from data/clues*.csv so repo clue banks are always scored
    csv_dir = ROOT / "data"
    if csv_dir.exists():
        import csv

        for csv_path in sorted(csv_dir.glob("clues*.csv")):
            with csv_path.open("r", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    cleaned = (row.get("answer") or "").strip().lower()
                    if ASCII_ALPHA_RE.match(cleaned):
                        answers.add(cleaned.upper())
    return answers


def score_word(word: str) -> float:
    val = round(float(zipf_frequency(word.lower(), "en")), 2)
    return val if val > 0.0 else DEFAULT_MISSING_SCORE


def main():
    blocklist = load_blocklist()
    raw_words = top_n_list("en", 65000)

    selected: set[str] = set()
    for w in raw_words:
        if not ASCII_ALPHA_RE.match(w):
            continue
        upper = w.upper()
        if upper in blocklist:
            continue
        selected.add(upper)

    for req in REQUIRED_WORDS:
        if req not in blocklist:
            selected.add(req)

    bank_answers = load_clue_bank_answers()
    sorted_words = sorted(selected)
    all_score_words = sorted(selected | bank_answers)
    scores = {word: score_word(word) for word in all_score_words}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_WORDS_PATH.write_text("\n".join(sorted_words) + "\n", encoding="utf-8")
    OUTPUT_SCORES_PATH.write_text(
        json.dumps(scores, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {len(sorted_words)} dictionary words and {len(scores)} scores "
        f"(including {len(bank_answers)} ClueBank answers) to {DATA_DIR}"
    )


if __name__ == "__main__":
    main()
