"""R-053: secrets never reach logs — redaction of records, args and token shapes."""

import logging

from goldtrader.notify.redact import MASK, Redactor, RedactingFilter, install, scan_for_secrets

BOT_TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ012345678"  # 9 digits : 35 chars
ANTHROPIC_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP"
MT5_PASSWORD = "Gold!Pass#2026"


def test_redactor_masks_registered_secrets_and_token_shapes():
    r = Redactor([MT5_PASSWORD])
    text = f"login ok pw={MT5_PASSWORD} token={BOT_TOKEN} key={ANTHROPIC_KEY} fine=hello"
    out = r.redact(text)
    assert MT5_PASSWORD not in out
    assert BOT_TOKEN not in out
    assert ANTHROPIC_KEY not in out
    assert out == f"login ok pw={MASK} token={MASK} key={MASK} fine=hello"


def test_redactor_ignores_short_secrets_and_handles_containers():
    r = Redactor(["1", "ab", ""])
    assert r.redact("1 ab 1") == "1 ab 1"
    r2 = Redactor([MT5_PASSWORD])
    assert r2.redact_value({"pw": MT5_PASSWORD, "n": 3, "l": [MT5_PASSWORD]}) == {
        "pw": MASK,
        "n": 3,
        "l": [MASK],
    }
    assert MT5_PASSWORD not in r2.redact_value(RuntimeError(f"bad {MT5_PASSWORD}"))


def test_logging_filter_masks_msg_args_and_exception_text(caplog):
    logger = logging.getLogger("goldtrader.test.redact")
    logger.propagate = True
    flt = install(logger, [MT5_PASSWORD])
    assert isinstance(flt, RedactingFilter)
    with caplog.at_level(logging.DEBUG, logger="goldtrader.test.redact"):
        logger.info("connecting with %s and %s", MT5_PASSWORD, BOT_TOKEN)
        logger.info("dict args %(pw)s", {"pw": MT5_PASSWORD})
        logger.info(f"inline {ANTHROPIC_KEY}")
        try:
            raise ValueError(f"boom {MT5_PASSWORD}")
        except ValueError:
            logger.exception("failed")
    text = caplog.text
    for rec in caplog.records:
        text += " " + rec.getMessage()
    assert scan_for_secrets(text, [MT5_PASSWORD]) == []
    assert MASK in text


def test_install_is_idempotent_and_covers_handlers():
    logger = logging.getLogger("goldtrader.test.redact2")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        install(logger, ["first-secret"])
        install(logger, ["second-secret"])
        assert sum(isinstance(f, RedactingFilter) for f in logger.filters) == 1
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
        flt = next(f for f in logger.filters if isinstance(f, RedactingFilter))
        assert flt.redactor.secrets == ("second-secret",)
    finally:
        logger.removeHandler(handler)


def test_scan_for_secrets_finds_leaks():
    text = f"a {BOT_TOKEN} b {MT5_PASSWORD} c {ANTHROPIC_KEY}"
    found = scan_for_secrets(text, [MT5_PASSWORD])
    assert set(found) == {BOT_TOKEN, MT5_PASSWORD, ANTHROPIC_KEY}
    assert scan_for_secrets("clean text 12345678:short", [MT5_PASSWORD]) == []
