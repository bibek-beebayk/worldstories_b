"""Pure transcript parsing: WebVTT / SubRip (SRT) / plain text -> ordered cues.

No Django / DRF / DB / storage imports — this module only operates on strings
already in memory, so it is directly unit-testable and safe to call from a live
request (parsing is fast; there is no background job, unlike epub_import).

Cross-cue semantic checks (overlaps, cues beyond the audio duration) live in
`serializers.validate_cue_sequence` so the import endpoint and a future cue-edit
endpoint share one validation path. This module owns only format/syntax sanity:
the header, timestamp shape, per-cue `end > start`, and "the file had no cues".
"""
import html
import re
from typing import NamedTuple

SUPPORTED_FORMATS = {"vtt", "srt", "text"}

_TIMESTAMP_RE = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]\d)[.,](\d{1,3})$")
# WebVTT inline timestamp tags (`<00:01.000>`) and any other angle-bracket tag
# (`<v Roger>`, `<c.loud>`, `<i>`, `<font color="#fff">`, closing tags, ...).
_TAG_RE = re.compile(r"<[^>]*>")
# SubRip / ASS brace directives (`{\an8}`, `{\i1}`).
_BRACE_RE = re.compile(r"\{[^}]*\}")
_WHITESPACE_RE = re.compile(r"\s+")


class TranscriptParseError(Exception):
    """Raised for any malformed transcript. Message is a single human string,
    prefixed with `Line N:` where a location is cheap to compute."""


class CueData(NamedTuple):
    order: int  # 1..N, assigned by position — the file's own index (if any) is ignored
    start_ms: int
    end_ms: int
    text: str  # one line: tags stripped, entities decoded, whitespace collapsed


class TranscriptParseResult(NamedTuple):
    cues: list  # list[CueData] — empty for plain text
    is_timed: bool
    transcript_html: str  # "<p>...</p>" of escaped cue/paragraph text; may be ""


def format_from_filename(name: str) -> "str | None":
    lowered = (name or "").lower()
    if lowered.endswith(".vtt"):
        return "vtt"
    if lowered.endswith(".srt"):
        return "srt"
    if lowered.endswith(".txt"):
        return "text"
    return None


def parse_transcript(content: str, fmt: str) -> TranscriptParseResult:
    if fmt not in SUPPORTED_FORMATS:
        raise TranscriptParseError(f"Unsupported transcript format: {fmt!r}.")
    try:
        if fmt == "vtt":
            return _parse_timed(content, fmt="vtt")
        if fmt == "srt":
            return _parse_timed(content, fmt="srt")
        return _parse_plain_text(content)
    except TranscriptParseError:
        raise
    except Exception as exc:  # pragma: no cover - defensive, mirrors epub_import
        raise TranscriptParseError(str(exc)) from exc


# --- helpers -----------------------------------------------------------------


def _lines(content: str) -> list:
    normalised = (content or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    return normalised.split("\n")


def _parse_timestamp(token: str, line_no: int) -> int:
    match = _TIMESTAMP_RE.match(token.strip())
    if not match:
        raise TranscriptParseError(f"Line {line_no}: malformed timestamp {token.strip()!r}.")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    fraction = match.group(4).ljust(3, "0")
    return ((hours * 3600) + (minutes * 60) + seconds) * 1000 + int(fraction)


def _clean_payload(lines: list) -> str:
    joined = "\n".join(lines)
    joined = _TAG_RE.sub("", joined)
    joined = _BRACE_RE.sub("", joined)
    joined = html.unescape(joined)
    return _WHITESPACE_RE.sub(" ", joined).strip()  # \s+ also collapses NBSP in Python 3


def _wrap_paragraphs(parts: list) -> str:
    return "".join(f"<p>{html.escape(part)}</p>" for part in parts if part)


def _blocks(lines: list) -> list:
    """Group lines into (start_line_no, [lines]) blocks split on blank-line runs."""
    blocks = []
    current = []
    current_start = 1
    for index, raw in enumerate(lines, start=1):
        if raw.strip():
            if not current:
                current_start = index
            current.append(raw)
        elif current:
            blocks.append((current_start, current))
            current = []
    if current:
        blocks.append((current_start, current))
    return blocks


def _parse_timing_line(line: str, line_no: int) -> "tuple[int, int]":
    left, sep, right = line.partition("-->")
    if not sep:
        raise TranscriptParseError(f"Line {line_no}: subtitle block has no timing line.")
    right_tokens = right.strip().split()
    if not right_tokens:
        raise TranscriptParseError(f"Line {line_no}: cue is missing an end time.")
    start_ms = _parse_timestamp(left, line_no)
    end_ms = _parse_timestamp(right_tokens[0], line_no)  # trailing cue settings / coords ignored
    if end_ms <= start_ms:
        raise TranscriptParseError(f"Line {line_no}: cue end time is not after its start time.")
    return start_ms, end_ms


def _finalise(raw_cues: list, source: str) -> TranscriptParseResult:
    if not raw_cues:
        raise TranscriptParseError(f"No subtitle cues found in the {source} file.")
    cues = [
        CueData(order=index, start_ms=start_ms, end_ms=end_ms, text=text)
        for index, (start_ms, end_ms, text) in enumerate(raw_cues, start=1)
    ]
    return TranscriptParseResult(
        cues=cues,
        is_timed=True,
        transcript_html=_wrap_paragraphs([cue.text for cue in cues]),
    )


# --- WebVTT / SubRip -----------------------------------------------------


def _parse_timed(content: str, *, fmt: str) -> TranscriptParseResult:
    lines = _lines(content)
    source = "WebVTT" if fmt == "vtt" else "SubRip"

    blocks = _blocks(lines)
    if fmt == "vtt":
        if not blocks:
            raise TranscriptParseError('Not a valid WebVTT file: missing "WEBVTT" header.')
        header_start, header_lines = blocks[0]
        header = header_lines[0].strip()
        if header != "WEBVTT" and not re.match(r"^WEBVTT[ \t]", header):
            raise TranscriptParseError('Not a valid WebVTT file: missing "WEBVTT" header.')
        blocks = blocks[1:]

    raw_cues = []
    for start_line, block_lines in blocks:
        first = block_lines[0].strip()
        if fmt == "vtt" and (first == "NOTE" or first.startswith("NOTE ") or first in {"STYLE", "REGION"}):
            continue

        timing_index = next((i for i, line in enumerate(block_lines) if "-->" in line), None)
        if timing_index is None:
            if fmt == "vtt":
                continue  # stray text between cues — ignore
            if all(not line.strip() or line.strip().isdigit() for line in block_lines):
                continue  # a bare SubRip index with no cue — tolerate
            raise TranscriptParseError(
                f"Line {start_line}: subtitle block has no timing line."
            )

        start_ms, end_ms = _parse_timing_line(
            block_lines[timing_index], start_line + timing_index
        )
        text = _clean_payload(block_lines[timing_index + 1 :])
        if not text:
            continue
        raw_cues.append((start_ms, end_ms, text))

    return _finalise(raw_cues, source)


# --- plain text --------------------------------------------------------


def _parse_plain_text(content: str) -> TranscriptParseResult:
    lines = _lines(content)
    paragraphs = [
        _WHITESPACE_RE.sub(" ", "\n".join(block_lines)).strip()
        for _, block_lines in _blocks(lines)
    ]
    return TranscriptParseResult(
        cues=[],
        is_timed=False,
        transcript_html=_wrap_paragraphs(paragraphs),
    )
