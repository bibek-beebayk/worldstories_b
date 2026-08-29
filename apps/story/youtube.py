import re
from urllib.parse import parse_qs, urlparse

_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_youtube_id(value):
    """Extract the 11-char YouTube video id from any common URL form
    (watch?v=, youtu.be/, /embed/, /shorts/, /live/) or from a bare id.

    Returns the id string, or None if nothing usable could be found.
    """
    if not value:
        return None
    value = value.strip()

    if _YT_ID_RE.match(value):
        return value

    parsed = urlparse(value if "//" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or ""

    if host in ("youtu.be", "youtube.be"):
        candidate = path.lstrip("/").split("/")[0]
        return candidate if _YT_ID_RE.match(candidate) else None

    if host in ("youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
            return candidate if _YT_ID_RE.match(candidate) else None
        for prefix in ("/embed/", "/shorts/", "/live/", "/v/"):
            if path.startswith(prefix):
                candidate = path[len(prefix):].split("/")[0]
                return candidate if _YT_ID_RE.match(candidate) else None

    return None


def parse_duration_seconds(value):
    """Accept an int/float of seconds, or a "mm:ss" / "hh:mm:ss" string.
    Returns a float, or None if blank/unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    try:
        return float(text)
    except ValueError:
        return None
