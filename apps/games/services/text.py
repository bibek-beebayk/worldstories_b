import html
import re
import unicodedata

import nh3

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘(\[]?[A-Z0-9])")
_ROMAN_NUMERAL_RE = re.compile(r"^[IVXLCDM]+\.?$", re.IGNORECASE)
_HEADING_PREFIX_RE = re.compile(r"^(chapter|part|book|section|prologue|epilogue)\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")


def html_to_plain_text(html_content: str) -> str:
    if not html_content:
        return ""
    # Preserve paragraph/line breaks before stripping tags
    with_breaks = re.sub(r"</?(p|div|br|h[1-6]|li|blockquote)[^>]*>", "\n", str(html_content), flags=re.IGNORECASE)
    stripped = nh3.clean(with_breaks, tags=set(), attributes={})
    unescaped = html.unescape(stripped)
    lines = [" ".join(line.split()) for line in unescaped.splitlines()]
    return "\n".join(line for line in lines if line)


def is_heading_like(line: str) -> bool:
    cleaned = line.strip()
    if not cleaned:
        return True
    if _HEADING_PREFIX_RE.match(cleaned):
        return True
    if _ROMAN_NUMERAL_RE.match(cleaned):
        return True
    letters = [ch for ch in cleaned if ch.isalpha()]
    if len(letters) >= 2 and all(ch.isupper() for ch in letters):
        return True
    return False


def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    sentences: list[str] = []
    for paragraph in text.splitlines():
        para = " ".join(paragraph.split()).strip()
        if not para or is_heading_like(para):
            continue
        parts = _SENTENCE_SPLIT_RE.split(para)
        for part in parts:
            s = " ".join(part.split()).strip()
            if s and not is_heading_like(s):
                sentences.append(s)
    return sentences


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


def normalize_for_leak(value: str) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    cleaned = _NON_ALNUM_RE.sub(" ", lowered)
    return " ".join(cleaned.split())


def format_initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s\-]+", (name or "").strip()) if p]
    letters = []
    for part in parts:
        alnum = next((ch for ch in part if ch.isalpha()), "")
        if alnum:
            letters.append(f"{alnum.upper()}.")
    return " ".join(letters)


def has_balanced_quotes(text: str) -> bool:
    if text.count('"') % 2 != 0:
        return False
    if text.count("\u201c") != text.count("\u201d"):
        return False
    return True


def starts_lowercase(text: str) -> bool:
    stripped = (text or "").lstrip(" \"'\u201c\u2018([")
    if not stripped:
        return True
    return stripped[0].islower()
