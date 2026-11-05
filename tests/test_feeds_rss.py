"""RSS / Atom parsing and conditional fetching (D-038): ids, dates, HTML stripping, 304 handling."""

from __future__ import annotations

import io
import urllib.error
from email.message import Message

from goldtrader.feeds.rss import (
    BODY_MAX_CHARS,
    USER_AGENT,
    FetchResult,
    fetch_feed,
    parse_date_ms,
    parse_feed,
    strip_html,
)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Federal Reserve Board: Press Releases</title>
  <link>https://www.federalreserve.gov</link>
  <item>
    <title>Federal Reserve issues FOMC statement &amp; projections</title>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20261105a.htm</link>
    <guid isPermaLink="false">monetary20261105a</guid>
    <pubDate>Thu, 05 Nov 2026 14:00:00 GMT</pubDate>
    <description><![CDATA[<p>The Committee decided to <b>maintain</b> the target range &amp; noted
      <a href="x">risks</a>.</p><script>alert("ignored")</script><br/>Second&nbsp;paragraph.]]></description>
  </item>
  <item>
    <title>No guid, bad date</title>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/other20261104b.htm</link>
    <pubDate>not a date at all</pubDate>
    <description>Plain description</description>
  </item>
  <item>
    <title>Title only with dc:date</title>
    <dc:date>2026-11-04T09:15:00-05:00</dc:date>
  </item>
  <item>
    <description>no title and no link: skipped</description>
  </item>
</channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>BLS latest releases</title>
  <updated>2026-11-05T13:30:00Z</updated>
  <entry>
    <id>urn:bls:empsit:2026-10</id>
    <title type="html">Employment Situation &#8212; October 2026</title>
    <link rel="self" href="https://www.bls.gov/feed/self"/>
    <link rel="alternate" type="text/html" href="https://www.bls.gov/news.release/empsit.nr0.htm"/>
    <published>2026-11-06T13:30:00Z</published>
    <updated>2026-11-06T13:35:00Z</updated>
    <summary type="html">&lt;p&gt;Total nonfarm payroll employment &lt;em&gt;rose&lt;/em&gt; by 150,000.&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>Entry without id or dates</title>
    <link href="https://www.bls.gov/news.release/cpi.nr0.htm"/>
    <content type="text">CPI content body</content>
  </entry>
</feed>
"""


def test_rss_items_ids_dates_and_html_body():
    items, err = parse_feed(RSS, "fed_press")
    assert err is None
    assert [i.item_id for i in items][:1] == ["fed_press:monetary20261105a"]
    first = items[0]
    assert first.source == "fed_press"
    assert first.title == "Federal Reserve issues FOMC statement & projections"
    assert first.link.endswith("monetary20261105a.htm")
    assert first.published_ms == 1_793_887_200_000  # 2026-11-05 14:00 UTC
    assert "<" not in first.body and "alert" not in first.body
    assert first.body == (
        "The Committee decided to maintain the target range & noted risks. Second paragraph."
    )
    # missing guid → hash of the link, bad date → None (store dates it at fetch time)
    second = items[1]
    assert second.item_id.startswith("fed_press:") and len(second.item_id) == len("fed_press:") + 32
    assert second.published_ms is None and second.body == "Plain description"
    # dc:date (ISO with offset) is read; title-only item gets a hash id too
    third = items[2]
    assert third.published_ms == 1_793_801_700_000  # 2026-11-04 14:15 UTC
    assert third.link == "" and third.item_id != second.item_id
    # the item with neither title nor link is skipped
    assert len(items) == 3


def test_rss_ids_are_deterministic():
    a, _ = parse_feed(RSS, "fed_press")
    b, _ = parse_feed(RSS, "fed_press")
    assert [i.item_id for i in a] == [i.item_id for i in b]
    c, _ = parse_feed(RSS, "other")
    assert all(i.item_id.startswith("other:") for i in c)


def test_atom_entries():
    items, err = parse_feed(ATOM, "bls_latest")
    assert err is None and len(items) == 2
    e = items[0]
    assert e.item_id == "bls_latest:urn:bls:empsit:2026-10"
    assert e.title == "Employment Situation — October 2026"
    assert e.link == "https://www.bls.gov/news.release/empsit.nr0.htm"  # alternate, not self
    assert e.published_ms == 1_793_971_800_000  # published wins over updated
    assert e.body == "Total nonfarm payroll employment rose by 150,000."
    other = items[1]
    assert other.published_ms is None and other.body == "CPI content body"
    assert other.link.endswith("cpi.nr0.htm") and other.item_id.startswith("bls_latest:")


def test_malformed_and_foreign_xml_return_error_not_exception():
    items, err = parse_feed("<rss><channel><item><title>oops", "x")
    assert items == [] and err and "parse error" in err
    items, err = parse_feed("<html><body>not a feed</body></html>", "x")
    assert items == [] and err and "no <item>" in err
    items, err = parse_feed("", "x")
    assert items == [] and err == "empty document"
    items, err = parse_feed(b"\xef\xbb\xbf" + RSS.encode(), "x")  # BOM + bytes
    assert err is None and len(items) == 3


def test_body_truncated_to_1500_and_entities_unescaped():
    long = "<p>" + ("word &amp; " * 400) + "</p>"
    rss = f"<rss><channel><item><title>t</title><link>http://a</link><description><![CDATA[{long}]]></description></item></channel></rss>"
    items, _ = parse_feed(rss, "s")
    assert len(items[0].body) <= BODY_MAX_CHARS and items[0].body.endswith("…")
    assert items[0].body.startswith("word & word &")
    assert strip_html("a<br>b &lt;c&gt; <style>x{}</style>") == "a b <c>"


def test_date_parsing_variants():
    assert parse_date_ms("Thu, 05 Nov 2026 14:00:00 GMT") == 1_793_887_200_000
    assert parse_date_ms("Thu, 05 Nov 2026 09:00:00 -0500") == 1_793_887_200_000
    assert parse_date_ms("2026-11-05T14:00:00Z") == 1_793_887_200_000
    assert parse_date_ms("2026-11-05T14:00:00") == 1_793_887_200_000  # naive → UTC
    assert parse_date_ms("2026-11-05 09:00:00-05:00") == 1_793_887_200_000
    assert parse_date_ms("yesterday") is None and parse_date_ms("") is None
    assert parse_date_ms(None) is None


# --- fetch -----------------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]):
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k] = v
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def close(self) -> None:
        self.closed = True


class _Opener:
    """Returns 200 with validators on the first call, raises HTTPError 304 when they come back."""

    def __init__(self) -> None:
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if req.get_header("If-none-match") == '"abc"':
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", Message(), None)
        return _Resp(
            200,
            RSS.encode("utf-8"),
            {
                "ETag": '"abc"',
                "Last-Modified": "Thu, 05 Nov 2026 14:00:00 GMT",
                "Content-Type": "application/rss+xml; charset=utf-8",
            },
        )


def test_fetch_200_then_304_with_validators_and_user_agent():
    op = _Opener()
    first = fetch_feed("https://example.test/feed.xml", opener=op, timeout_s=3)
    assert first.ok and first.status == 200 and "<rss" in first.text
    assert first.etag == '"abc"' and first.last_modified == "Thu, 05 Nov 2026 14:00:00 GMT"
    req, timeout = op.requests[0]
    assert req.get_header("User-agent") == USER_AGENT and timeout == 3
    assert req.get_header("If-none-match") is None
    second = fetch_feed(
        "https://example.test/feed.xml",
        etag=first.etag,
        last_modified=first.last_modified,
        opener=op,
    )
    assert second.unchanged and second.status == 304 and second.text == ""
    assert second.etag == '"abc"' and second.last_modified == first.last_modified
    req2, _ = op.requests[1]
    assert req2.get_header("If-none-match") == '"abc"'
    assert req2.get_header("If-modified-since") == first.last_modified


def test_fetch_errors_are_returned_not_raised():
    class Boom:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("name resolution failed")

    res = fetch_feed("https://nowhere.test/x", opener=Boom())
    assert res.status == 0 and not res.ok and "name resolution" in (res.error or "")

    class NotFound:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", Message(), None)

    res = fetch_feed("https://nowhere.test/x", opener=NotFound())
    assert res.status == 404 and "404" in (res.error or "")

    class Slow:
        def open(self, req, timeout=None):
            raise TimeoutError("timed out")

    res = fetch_feed("https://nowhere.test/x", opener=Slow())
    assert res.status == 0 and "timeout" in (res.error or "")

    def open_fn(req, timeout):
        return _Resp(200, b"<rss/>", {})

    res = fetch_feed("https://x.test/", open_fn=open_fn)
    assert res == FetchResult(200, "<rss/>", etag=None, last_modified=None)
