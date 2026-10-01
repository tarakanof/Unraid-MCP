"""Tests for stderr logging and API-key redaction."""

from __future__ import annotations

import logging

import pytest

from unraid_mcp import logging as unraid_logging
from unraid_mcp.logging import RedactionFilter, configure_logging, get_logger, redact

KEY = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"


def test_redaction_filter_scrubs_secret_from_message():
    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        f"calling with key {KEY}",
        None,
        None,
    )
    assert RedactionFilter(KEY).filter(record) is True
    assert KEY not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_redaction_filter_scrubs_interpolated_secret():
    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "key=%s done",
        (KEY,),
        None,
    )
    RedactionFilter(KEY).filter(record)
    assert KEY not in record.getMessage()


def test_redaction_filter_noop_for_empty_or_short_secret():
    for secret in (None, "", "abc"):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "abc def", None, None)
        assert RedactionFilter(secret).filter(record) is True
        assert record.getMessage() == "abc def"


def test_configure_logging_emits_to_stderr_and_redacts(capsys):
    configure_logging(level="INFO", api_key=KEY)
    get_logger("unraid_mcp.test").info(f"using key {KEY} now")
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout — protocol channel must stay clean
    assert KEY not in captured.err
    assert "***REDACTED***" in captured.err


def test_configure_logging_redacts_secret_in_traceback(capsys):
    configure_logging(level="INFO", api_key=KEY)
    try:
        raise RuntimeError(f"boom with {KEY} inside")
    except RuntimeError:
        get_logger("unraid_mcp.test").exception("request failed")
    captured = capsys.readouterr()
    assert KEY not in captured.err
    assert "***REDACTED***" in captured.err


def test_configure_logging_is_idempotent(capsys):
    configure_logging(level="INFO", api_key=KEY)
    configure_logging(level="INFO", api_key=KEY)
    get_logger("unraid_mcp.test").info("hello")
    # Exactly one line => handlers not duplicated.
    assert captured_lines(capsys) == 1


def captured_lines(capsys) -> int:
    err = capsys.readouterr().err.strip()
    return len([line for line in err.splitlines() if "hello" in line])


@pytest.mark.parametrize("secret", [KEY, "bearer-token-1234567890123456789012", 'a\n"b-secret'])
def test_redact_nested_containers_and_representations(secret):
    class Echo:
        def __str__(self):
            return f"echo {secret}"

    value = {secret: [None, (f"prefix {secret}", {"echo": Echo()})], "number": 42}
    result = redact(value, [secret, None, ""])
    assert result == {
        "***REDACTED***": [None, ("prefix ***REDACTED***", {"echo": "echo ***REDACTED***"})],
        "number": 42,
    }
    assert secret in value


def test_redact_clean_values_pass_through_unchanged():
    value = {"items": [None, ("safe", 1, False)]}
    assert redact(value, [KEY]) is value
    assert redact(value, []) is value


def test_configure_logging_redacts_bearer_token_in_traceback(capsys):
    token = "bearer-token-1234567890123456789012"
    configure_logging(secrets=[token])
    try:
        raise RuntimeError(f"reflected {token}")
    except RuntimeError:
        get_logger("unraid_mcp.test").exception("request failed with %s", token)
    captured = capsys.readouterr()
    assert token not in captured.err
    assert "***REDACTED***" in captured.err
    assert captured.out == ""


def test_websockets_frames_never_logged_even_at_debug(capsys):
    import logging

    from websockets.frames import Frame, Opcode

    configure_logging("DEBUG", KEY)
    frame = Frame(Opcode.TEXT, f'{{"payload": "{KEY}", "q": "a\\"b"}}'.encode())
    logging.getLogger("websockets.client").debug("< %s", frame)
    logging.getLogger("websockets.client").info("handshake ok")
    err = capsys.readouterr().err
    assert KEY not in err
    assert "TEXT" not in err
    assert "handshake ok" in err


@pytest.fixture
def fresh_short_warning(monkeypatch):
    monkeypatch.setattr(unraid_logging, "_warned_short_secret", False)


def test_redact_ignores_sub_floor_secret_and_warns_once(caplog, fresh_short_warning):
    value = "a toy key k appears in: k, key, kk"
    with caplog.at_level(logging.WARNING, logger="unraid_mcp.logging"):
        assert redact(value, ["k"]) == value
        assert redact({"x": value}, ["k", "abc"]) == {"x": value}
    warnings = [r for r in caplog.records if "shorter than" in r.getMessage()]
    assert len(warnings) == 1
    assert "k," not in warnings[0].getMessage()


def test_redact_sub_floor_does_not_block_long_secret(fresh_short_warning):
    assert redact(f"k {KEY} k", ["k", KEY]) == "k ***REDACTED*** k"


def test_configure_logging_warns_once_for_short_secret(capsys, fresh_short_warning):
    configure_logging(level="INFO", api_key="shrtkey")
    get_logger("unraid_mcp.test").info("hello shrtkey")
    err = capsys.readouterr().err
    assert err.count("shorter than") == 1
    assert "hello shrtkey" in err  # not scrubbed below the floor
