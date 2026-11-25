"""Dev-only script to generate apps/games/data/crossword_words.txt using wordfreq.

Usage (dev environment only; do NOT add wordfreq to production requirements):
    pip install wordfreq
    python scripts/build_crossword_wordlist.py
"""

from pathlib import Path
import re

from wordfreq import top_n_list

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "apps" / "games" / "data"
BLOCKLIST_PATH = DATA_DIR / "crossword_blocklist.txt"
OUTPUT_PATH = DATA_DIR / "crossword_words.txt"

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
    # Pull from top ~60,000 English words so we comfortably get 8,000+ clean 3-5 letter words
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(sorted_words) + "\n", encoding="utf-8")
    print(f"Wrote {len(sorted_words)} words to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
