"""RSS 2.0 / Atom parsing and conditional fetching with the standard library only (D-038).

* :func:`parse_feed` turns feed XML into :class:`FeedItem` rows for ``news_items``: stable ids,
  UTC publication times, and a body stripped of HTML and capped at 1,500 characters (the Research
  bot's excerpt limit, AGENTS.md §3.1). Malformed XML never raises: the caller gets ``([], error)``.
* :func:`fetch_feed` is the only function that touches the network. It sends ``If-None-Match`` /
  ``If-Modified-Since`` so unchanged feeds cost one 304, and takes an injectable ``opener`` so tests
  never open a socket.

Nothing here interprets the text: titles and bodies are data that only reaches a model through
``wrap_untrusted`` (R-024). Item provenance (source, link, published time) is kept for the journal
(R-030).
"""

from __future__ import annotations

import hashlib
import html
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree as ET

BODY_MAX_CHARS = 1500
"""AGENTS §3.1: body excerpt ≤ 1,500 chars."""

USER_AGENT = "goldtrader/0.1"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One feed entry, already normalised for ``news_items``.

    ``published_ms`` is None when the entry carries no parsable date; the store then uses the
    fetch time.
    """

    item_id: str
    source: str
    title: str
    link: str
    published_ms: int | None
    body: str


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one conditional GET. ``status`` 0 means the request never got an HTTP answer."""

    status: int
    text: str
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None

    @property
    def unchanged(self) -> bool:
        return self.status == 304


# -- HTML stripping -------------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Collect text nodes, dropping script/style contents; entities are unescaped by the parser."""

    _SKIP = frozenset({"script", "style"})
    _BLOCK = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def strip_html(text: str) -> str:
    """Remove tags, unescape entities and collapse whitespace. Never raises on broken markup."""
    if not text:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # html.parser is lenient; this is belt and braces
        return " ".join(html.unescape(text).split())
    return " ".join("".join(parser.parts).split())


def truncate(text: str, max_chars: int = BODY_MAX_CHARS) -> str:
    """Cap ``text`` at ``max_chars`` (an ellipsis counts towards the cap)."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


# -- dates ------------------------------------------------------------------------------------------


def parse_date_ms(raw: str | None) -> int | None:
    """RFC 2822 (RSS ``pubDate``) or ISO 8601 (Atom ``updated``) → epoch ms UTC; None if unreadable.

    A naive timestamp is read as UTC. Garbage returns None rather than raising.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        return int(dt.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


# -- XML helpers --------------------------------------------------------------------------------------


def _local(tag: Any) -> str:
    """Local name of a possibly namespaced tag (``{ns}item`` → ``item``)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el: ET.Element, *names: str) -> str | None:
    """Text of the first direct child whose local name is in ``names`` (first non-empty wins)."""
    wanted = {n.lower() for n in names}
    for child in el:
        if _local(child.tag) in wanted:
            text = "".join(child.itertext()).strip() if child.text or len(child) else ""
            if not text and child.text:
                text = child.text.strip()
            if text:
                return text
    return None


def _atom_link(el: ET.Element) -> str:
    """Atom ``<link href>``: prefer rel=alternate (or no rel), fall back to the first href."""
    first = ""
    for child in el:
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            # RSS-style <link>text</link> inside an entry
            href = (child.text or "").strip()
        if not href:
            continue
        rel = (child.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href
        first = first or href
    return first


def _entry_elements(root: ET.Element) -> tuple[str, list[ET.Element]]:
    """``("rss", items)`` or ``("atom", entries)``; detected by content, not by root name."""
    items = [el for el in root.iter() if _local(el.tag) == "item"]
    if items:
        return "rss", items
    entries = [el for el in root.iter() if _local(el.tag) == "entry"]
    if entries:
        return "atom", entries
    return "unknown", []


def _make_id(source: str, guid: str | None, link: str, title: str, published_ms: int | None) -> str:
    if guid:
        return f"{source}:{guid.strip()}"
    basis = link.strip() or f"{title.strip()}|{published_ms if published_ms is not None else ''}"
    return f"{source}:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# -- parse -----------------------------------------------------------------------------------------


def parse_feed(text: str | bytes, source: str) -> tuple[list[FeedItem], str | None]:
    """Parse RSS 2.0 (also RSS 1.0 / RDF items) or Atom into :class:`FeedItem` rows.

    Returns ``(items, error)``. ``error`` is None on success; on malformed XML, or when the document
    holds neither ``<item>`` nor ``<entry>`` elements, ``items`` is empty and ``error`` says why.
    Per item: ``item_id`` = ``<source>:<guid|id>`` when present, else ``<source>:sha256(link or
    title+published)``; ``published_ms`` from pubDate / dc:date (RSS) or published / updated /
    issued (Atom); ``body`` = description / summary / content with HTML stripped, entities
    unescaped and ≤ 1,500 chars. Entries with neither title nor link are skipped.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    stripped = text.lstrip("﻿ \t\r\n")
    if not stripped:
        return [], "empty document"
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as e:
        return [], f"xml parse error: {e}"
    kind, elements = _entry_elements(root)
    if kind == "unknown":
        return [], "no <item> or <entry> elements found"
    out: list[FeedItem] = []
    seen: set[str] = set()
    for el in elements:
        title = strip_html(_child_text(el, "title") or "")
        if kind == "rss":
            link = (_child_text(el, "link") or "").strip()
            guid = _child_text(el, "guid")
            published_ms = parse_date_ms(_child_text(el, "pubDate", "date", "pubdate"))
            body_raw = _child_text(el, "description", "encoded", "summary", "content") or ""
        else:
            link = _atom_link(el)
            guid = _child_text(el, "id")
            published_ms = parse_date_ms(_child_text(el, "published", "updated", "issued"))
            body_raw = _child_text(el, "content", "summary") or ""
        if not title and not link:
            continue
        item_id = _make_id(source, guid, link, title, published_ms)
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append(
            FeedItem(
                item_id=item_id,
                source=source,
                title=truncate(title, 500),
                link=link,
                published_ms=published_ms,
                body=truncate(strip_html(body_raw)),
            )
        )
    return out, None


# -- fetch -----------------------------------------------------------------------------------------

Opener = Any
"""Anything with ``open(request, timeout=...)`` returning a response with ``status``/``getcode()``,
``headers`` and ``read()`` — ``urllib.request.OpenerDirector`` or a test double."""


def _header(resp: Any, name: str) -> str | None:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    value = headers.get(name) if hasattr(headers, "get") else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _status(resp: Any) -> int:
    status = getattr(resp, "status", None)
    if status is None and hasattr(resp, "getcode"):
        status = resp.getcode()
    return int(status or 200)


def _decode(body: bytes, resp: Any) -> str:
    charset = None
    headers = getattr(resp, "headers", None)
    if headers is not None and hasattr(headers, "get_content_charset"):
        charset = headers.get_content_charset()
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch_feed(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_s: float = 10,
    opener: Opener | None = None,
    open_fn: Callable[[urllib.request.Request, float], Any] | None = None,
) -> FetchResult:
    """Conditional GET of ``url`` with a plain ``User-Agent`` (D-038). Never raises.

    * 200 → ``FetchResult(200, text, etag, last_modified)`` with the response validators;
    * 304 → ``FetchResult(304, "", etag, last_modified)`` echoing the validators we sent (nothing
      changed; the caller keeps its stored state);
    * any other HTTP status → that status, empty text and ``error``;
    * network / timeout / TLS failures → status 0 and ``error``.

    ``opener`` (an ``OpenerDirector``-like object) or ``open_fn(request, timeout)`` lets tests
    inject the transport; the default is ``urllib.request.build_opener()``.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        if open_fn is not None:
            resp = open_fn(req, timeout_s)
        else:
            director = opener if opener is not None else urllib.request.build_opener()
            resp = director.open(req, timeout=timeout_s)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return FetchResult(304, "", etag=etag, last_modified=last_modified)
        return FetchResult(int(e.code), "", error=f"http {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        return FetchResult(0, "", error=f"url error: {e.reason}")
    except TimeoutError as e:
        return FetchResult(0, "", error=f"timeout: {e or 'read timed out'}")
    except Exception as e:  # the fetch loop must survive anything the transport throws
        return FetchResult(0, "", error=f"{type(e).__name__}: {e}")
    try:
        status = _status(resp)
        if status == 304:
            return FetchResult(304, "", etag=etag, last_modified=last_modified)
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return FetchResult(status, "", error=f"response larger than {MAX_RESPONSE_BYTES} bytes")
        text = _decode(body, resp)
        if status != 200:
            return FetchResult(status, "", error=f"http {status}")
        return FetchResult(
            200, text, etag=_header(resp, "ETag"), last_modified=_header(resp, "Last-Modified")
        )
    except Exception as e:
        return FetchResult(0, "", error=f"read failed: {type(e).__name__}: {e}")
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
