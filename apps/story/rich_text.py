import html

import nh3

from .epub_import import ALLOWED_ATTRIBUTES, ALLOWED_TAGS


def sanitize_reader_html(value) -> str:
    """Return reader-safe rich text using the same allow-list as imported chapters."""
    return nh3.clean(
        str(value or ""),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        # ``rel`` is already explicitly controlled by ALLOWED_ATTRIBUTES;
        # nh3 rejects combining that with its automatic link_rel option.
        link_rel=None,
    )


def rich_text_has_content(value) -> bool:
    """Treat markup-only editor values such as ``<p><br></p>`` as empty."""
    sanitized = sanitize_reader_html(value)
    plain_text = nh3.clean(sanitized, tags=set(), attributes={})
    return bool(html.unescape(plain_text).replace("\u00a0", " ").strip())
