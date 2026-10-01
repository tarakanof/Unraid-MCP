"""Tests for stderr logging and API-key redaction."""

from __future__ import annotations

import logging

import pytest

from unraid_mcp.logging import RedactionFilter, configure_logging, get_logger, redact


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


def test_configure_logging_redacts_secret_in_traceback(capsys):
    configure_logging(level="INFO", api_key="supersecretkey123")
    try:
        raise RuntimeError("boom with supersecretkey123 inside")
    except RuntimeError:
        get_logger("unraid_mcp.test").exception("request failed")
    captured = capsys.readouterr()
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


@pytest.mark.parametrize(
    "secret", ["supersecretkey123", "bearer-token-1234567890123456789012", 'a\n"b']
)
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
    assert redact(value, ["supersecretkey123"]) is value
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

    configure_logging("DEBUG", "supersecretkey123")
    frame = Frame(Opcode.TEXT, b'{"payload": "supersecretkey123", "q": "a\\"b"}')
    logging.getLogger("websockets.client").debug("< %s", frame)
    logging.getLogger("websockets.client").info("handshake ok")
    err = capsys.readouterr().err
    assert "supersecretkey123" not in err
    assert "TEXT" not in err
    assert "handshake ok" in err
