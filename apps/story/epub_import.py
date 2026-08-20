"""Pure EPUB -> chapter-list parsing. No Django/DB/storage I/O in this module
— it only ever operates on bytes already in memory, so it's directly unit
testable and safely reusable from both a live request and a background
thread (see epub_import_jobs.py for the DB-touching side of the pipeline).
"""
import io
import re
import zipfile
from typing import NamedTuple
from urllib.parse import unquote, urljoin

import nh3
from ebooklib import epub
from lxml import html as lxml_html

# Mirrors the frontend's ALLOWED_TAGS (src/lib/sanitizeHtml.ts), minus img
# (images are stripped entirely from epub-derived chapters for this version —
# extracting/re-uploading embedded images to R2 is a deliberate v2 scope cut)
# and minus the handful of layout tags (article/aside/nav/header/footer/
# section/main) that only make sense as page-level structure, not inside a
# single chapter's body content.
ALLOWED_TAGS = {
    "a", "abbr", "address", "b", "bdi", "bdo", "blockquote", "br", "caption",
    "cite", "code", "col", "colgroup", "data", "dd", "del", "details", "dfn",
    "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q",
    "rp", "rt", "ruby", "s", "samp", "small", "span", "strong", "sub",
    "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time",
    "tr", "u", "ul", "var", "wbr",
}
ALLOWED_ATTRIBUTES = {
    "*": {"class", "id", "title", "lang", "dir"},
    "a": {"href", "name", "target", "rel"},
    "time": {"datetime"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "ol": {"start", "type"},
}

# EPUB "guide" reference types that mark front/back matter rather than real
# reading content — https://idpf.org/epub/20/spec/OPF_2.0.1_draft.htm#Section2.6
NON_CHAPTER_GUIDE_TYPES = {
    "cover", "title-page", "toc", "index", "glossary", "copyright-page",
    "acknowledgements", "colophon", "epigraph", "notes", "loi", "lot",
}


class EpubParseError(Exception):
    pass


class ChapterData(NamedTuple):
    order: int
    title: str
    content_html: str


def _validate_epub_container(epub_bytes: bytes) -> None:
    """OCF spec: a valid EPUB's first zip entry is an uncompressed
    'mimetype' file containing exactly 'application/epub+zip'. Cheap,
    dependency-free, and a much stronger signal than a file-extension or
    generic zip-magic-bytes check."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(epub_bytes))
        names = zf.namelist()
        if not names or names[0] != "mimetype":
            raise EpubParseError("Not a valid EPUB: missing OCF mimetype entry.")
        info = zf.getinfo("mimetype")
        if info.compress_type != zipfile.ZIP_STORED:
            raise EpubParseError("Not a valid EPUB: mimetype entry must be stored uncompressed.")
        if zf.read("mimetype") != b"application/epub+zip":
            raise EpubParseError("Not a valid EPUB: unexpected mimetype content.")
    except zipfile.BadZipFile as exc:
        raise EpubParseError(f"Not a valid EPUB (not a zip archive): {exc}") from exc


def _normalize_href(href: str) -> str:
    if not href:
        return ""
    href = href.split("#", 1)[0]
    return unquote(href).strip().lstrip("./")


def _build_toc_title_map(book) -> dict:
    """Flattens book.toc (which transparently represents both EPUB2 NCX and
    EPUB3 NAV formats as nested epub.Link / list structures) into
    normalized-href -> title."""
    title_map = {}

    def _walk(entries):
        for entry in entries:
            if isinstance(entry, (list, tuple)):
                _walk(entry)
                continue
            href = getattr(entry, "href", None)
            title = getattr(entry, "title", None)
            if href and title:
                title_map[_normalize_href(href)] = title

    _walk(book.toc)
    return title_map


def _is_excluded_item(item, guide_hrefs) -> bool:
    if isinstance(item, (epub.EpubNav, epub.EpubCoverHtml)):
        return True
    if _normalize_href(getattr(item, "file_name", "")) in guide_hrefs:
        return True
    return False


def _extract_body_html(item) -> str:
    """get_body_content() is inconsistent across ebooklib versions/source
    EPUBs about whether it returns a bare fragment (possibly multiple
    top-level sibling tags) or the full <html><body>...</body></html>
    wrapper. lxml.html.fromstring() assumes a single root element, so on a
    bare multi-sibling fragment it silently returns only the first element's
    *children* — dropping the top-level tag(s) entirely. fragments_fromstring
    is the correct lxml API for this: it explicitly supports multiple
    top-level fragments and unwraps a full <html>/<body> document down to
    just the body content on its own."""
    raw = item.get_body_content()
    # get_body_content() returns bytes with no encoding declaration of its
    # own (that lives in the full document's <head>, which this fragment
    # doesn't have) — EPUB content documents are required to be UTF-8 or
    # UTF-16 (almost always UTF-8 in practice), but fragments_fromstring on
    # *undecoded* bytes lets libxml2 guess, which defaults to a Latin-1-style
    # byte-for-char mapping and silently mangles every multi-byte character
    # (curly quotes, em dashes, accented letters) into mojibake. Decoding to
    # str first sidesteps the guess entirely.
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    try:
        fragments = lxml_html.fragments_fromstring(raw)
    except Exception:
        return raw

    return "".join(
        frag if isinstance(frag, str) else lxml_html.tostring(frag, encoding="unicode")
        for frag in fragments
    )


def _title_from_heading(html_fragment: str) -> str | None:
    try:
        tree = lxml_html.fromstring(f"<div>{html_fragment}</div>")
    except Exception:
        return None
    for tag in ("h1", "h2"):
        found = tree.find(f".//{tag}")
        if found is not None:
            text = found.text_content().strip()
            if text:
                return text
    return None


def _strip_images_and_unwrap_internal_links(html_fragment: str, own_href: str) -> str:
    try:
        tree = lxml_html.fromstring(f"<div>{html_fragment}</div>")
    except Exception:
        return html_fragment

    for img in tree.findall(".//img"):
        img.getparent().remove(img)

    for link in tree.findall(".//link") + tree.findall(".//style"):
        link.getparent().remove(link)

    for anchor in tree.findall(".//a"):
        href = anchor.get("href") or ""
        resolved = _normalize_href(urljoin(own_href, href)) if href else ""
        is_internal = href and not href.startswith(("http://", "https://", "mailto:", "tel:"))
        if is_internal and (not resolved or "." in resolved.rsplit("/", 1)[-1]):
            anchor.drop_tag()

    return "".join(lxml_html.tostring(child, encoding="unicode") for child in tree)


def extract_chapters(epub_bytes: bytes) -> list[ChapterData]:
    try:
        _validate_epub_container(epub_bytes)
        book = epub.read_epub(io.BytesIO(epub_bytes))

        guide_hrefs = {
            _normalize_href(entry.get("href", ""))
            for entry in (book.guide or [])
            if entry.get("type") in NON_CHAPTER_GUIDE_TYPES
        }
        toc_titles = _build_toc_title_map(book)

        chapters = []
        order = 0
        for idref, linear in book.spine:
            if linear not in ("yes", "linear"):
                continue
            item = book.get_item_with_id(idref)
            if item is None or not isinstance(item, epub.EpubHtml):
                continue
            if _is_excluded_item(item, guide_hrefs):
                continue

            own_href = _normalize_href(getattr(item, "file_name", ""))
            body_html = _extract_body_html(item)

            title = toc_titles.get(own_href) or _title_from_heading(body_html)
            order += 1
            if not title:
                title = f"Chapter {order}"

            content_html = _strip_images_and_unwrap_internal_links(body_html, own_href)
            content_html = nh3.clean(
                content_html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, link_rel=None
            )

            chapters.append(ChapterData(order=order, title=title, content_html=content_html))

        return chapters
    except EpubParseError:
        raise
    except Exception as exc:
        raise EpubParseError(str(exc)) from exc
