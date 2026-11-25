"""Sampling script (not run in normal test suite) to verify Mini Crossword
difficulty label spread across 2,000 random 10-answer sets from the clue bank.

Usage:
    python scripts/check_difficulty_spread.py
"""

from collections import Counter
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
import django

django.setup()

from apps.games.api_mini import import_clue_bank_csv_text
from apps.games.crossword.difficulty import (
    DIFFICULTY_BASE_ZIPF,
    DIFFICULTY_EASY_MAX_SCORE,
    DIFFICULTY_HARD_MIN_SCORE,
    DIFFICULTY_NAME_WEIGHT,
    DIFFICULTY_RARE_WEIGHT,
)
from apps.games.crossword.words import is_name, is_rare, zipf
from apps.games.models import ClueBank


def score_answer_set(answers: list[str], flagged_names: set[str]) -> tuple[str, float]:
    names = [a for a in answers if is_name(a, clue_bank_flag=(a in flagged_names))]
    name_set = set(names)
    rare_words = [a for a in answers if is_rare(a, clue_bank_flag=(a in flagged_names))]
    zipf_vals = [zipf(a, is_name_word=(a in name_set)) for a in answers]
    avg_zipf = round(sum(zipf_vals) / len(zipf_vals), 2)
    raw = (
        (DIFFICULTY_BASE_ZIPF - avg_zipf)
        + DIFFICULTY_RARE_WEIGHT * len(rare_words)
        + DIFFICULTY_NAME_WEIGHT * max(0, len(names) - 1)
    )
    score = round(max(0.0, raw), 2)
    if score < DIFFICULTY_EASY_MAX_SCORE:
        return "easy", score
    if score >= DIFFICULTY_HARD_MIN_SCORE:
        return "hard", score
    return "medium", score


def load_bank_answers() -> tuple[list[str], set[str], str]:
    rows = list(ClueBank.objects.values_list("answer", "is_name"))
    source = "database ClueBank"
    if len(rows) < 20:
        # Fallback to data/clues.csv if local DB ClueBank is empty
        csv_path = ROOT / "data" / "clues.csv"
        if csv_path.exists():
            import csv

            rows = []
            with csv_path.open("r", encoding="utf-8") as fh:
                for rec in csv.DictReader(fh):
                    ans = (rec.get("answer") or "").strip().upper()
                    flag = (rec.get("is_name") or "").strip().lower() in ("true", "1", "yes")
                    if ans and 3 <= len(ans) <= 5 and ans.isalpha():
                        rows.append((ans, flag))
            source = f"CSV fallback ({csv_path.name})"

    distinct: dict[str, bool] = {}
    for ans, flag in rows:
        up = (ans or "").strip().upper()
        if up and 3 <= len(up) <= 5 and up.isalpha():
            distinct[up] = distinct.get(up, False) or bool(flag)

    answers = sorted(distinct.keys())
    flagged = {a for a, f in distinct.items() if f}
    return answers, flagged, source


def main():
    answers, flagged, source = load_bank_answers()
    if len(answers) < 10:
        print(f"Not enough answers ({len(answers)}) in {source} to sample 10-answer sets.")
        return

    rng = random.Random(20261126)
    samples = 2000
    counts: Counter[str] = Counter()
    scores: list[float] = []

    for _ in range(samples):
        subset = rng.sample(answers, 10)
        label, sc = score_answer_set(subset, flagged)
        counts[label] += 1
        scores.append(sc)

    scores.sort()
    p25 = scores[int(samples * 0.25)]
    p50 = scores[int(samples * 0.50)]
    p75 = scores[int(samples * 0.75)]
    print(f"Source: {source} ({len(answers)} distinct answers, {len(flagged)} flagged names)")
    print(
        f"Thresholds: easy < {DIFFICULTY_EASY_MAX_SCORE}, hard >= {DIFFICULTY_HARD_MIN_SCORE} | "
        f"Score quartiles: p25={p25}, p50={p50}, p75={p75}"
    )
    for lbl in ("easy", "medium", "hard"):
        pct = round(counts[lbl] / samples * 100, 1)
        print(f"  {lbl:6s}: {counts[lbl]:4d} / {samples} ({pct}%)")


if __name__ == "__main__":
    main()
