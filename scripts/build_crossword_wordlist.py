"""Dev-only script to generate apps/games/data/crossword_words.txt and
apps/games/data/crossword_word_scores.json using wordfreq.

Usage (dev environment only; do NOT add wordfreq to production requirements):
    pip install wordfreq
    python scripts/build_crossword_wordlist.py
"""

import json
from pathlib import Path
import re

from wordfreq import top_n_list, zipf_frequency

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "apps" / "games" / "data"
BLOCKLIST_PATH = DATA_DIR / "crossword_blocklist.txt"
OUTPUT_WORDS_PATH = DATA_DIR / "crossword_words.txt"
OUTPUT_SCORES_PATH = DATA_DIR / "crossword_word_scores.json"

ASCII_ALPHA_RE = re.compile(r"^[a-z]{3,5}$")

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

    sorted_words = sorted(selected)
    scores = {
        word: round(float(zipf_frequency(word.lower(), "en")), 2)
        for word in sorted_words
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_WORDS_PATH.write_text("\n".join(sorted_words) + "\n", encoding="utf-8")
    OUTPUT_SCORES_PATH.write_text(
        json.dumps(scores, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(sorted_words)} words and scores to {DATA_DIR}")


if __name__ == "__main__":
    main()
