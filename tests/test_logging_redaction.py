"""Tests for stderr logging and API-key redaction."""

from __future__ import annotations

import logging

from unraid_mcp.logging import RedactionFilter, configure_logging, get_logger


def test_redaction_filter_scrubs_secret_from_message():
    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "calling with key supersecretkey123",
        None,
        None,
    )
    assert RedactionFilter("supersecretkey123").filter(record) is True
    assert "supersecretkey123" not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_redaction_filter_scrubs_interpolated_secret():
    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "key=%s done",
        ("supersecretkey123",),
        None,
    )
    RedactionFilter("supersecretkey123").filter(record)
    assert "supersecretkey123" not in record.getMessage()


def test_redaction_filter_noop_for_empty_or_short_secret():
    for secret in (None, "", "abc"):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "abc def", None, None)
        assert RedactionFilter(secret).filter(record) is True
        assert record.getMessage() == "abc def"


def test_configure_logging_emits_to_stderr_and_redacts(capsys):
    configure_logging(level="INFO", api_key="supersecretkey123")
    get_logger("unraid_mcp.test").info("using key supersecretkey123 now")
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout — protocol channel must stay clean
    assert "supersecretkey123" not in captured.err
    assert "***REDACTED***" in captured.err


def test_configure_logging_is_idempotent(capsys):
    configure_logging(level="INFO", api_key="supersecretkey123")
    configure_logging(level="INFO", api_key="supersecretkey123")
    get_logger("unraid_mcp.test").info("hello")
    # Exactly one line => handlers not duplicated.
    assert captured_lines(capsys) == 1


def captured_lines(capsys) -> int:
    err = capsys.readouterr().err.strip()
    return len([line for line in err.splitlines() if "hello" in line])
