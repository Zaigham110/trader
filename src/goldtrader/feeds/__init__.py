"""News and calendar feed pipeline (D-038): RSS/Atom → `news_items`, calendars → `events`.

Pure parsers live in :mod:`goldtrader.feeds.rss` and :mod:`goldtrader.session.calendar`; the stores
and fetchers here only move data into SQLite. Nothing in this package touches MT5, the execution
engine or a model: raw news text leaves the database only through the Research bot's defanged
``<untrusted_news>`` blocks (R-024).
"""
