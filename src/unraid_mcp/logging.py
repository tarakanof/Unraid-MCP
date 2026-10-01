"""Logging configuration for the Unraid MCP server.

Two invariants matter here:

1. **Everything goes to stderr.** On the stdio transport, stdout carries the
   JSON-RPC stream, so any stray log line on stdout corrupts the protocol.
2. **The API key is never logged.** A redaction filter scrubs the configured
   secret from every record before it is emitted, as defence in depth on top
   of using ``SecretStr`` everywhere else.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from typing import Any

_REDACTION = "***REDACTED***"


def redact(value: Any, secrets: Iterable[str | None]) -> Any:
    """Scrub strings, containers and object representations using configured secrets.

    Empty secrets are ignored. Clean strings and containers pass through unchanged.
    """
    configured = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    if not configured:
        return value

    def scrub(item: Any) -> Any:
        if isinstance(item, str):
            for secret in configured:
                if secret in item:
                    item = item.replace(secret, _REDACTION)
            return item
        if isinstance(item, dict):
            pairs = [(scrub(k), scrub(v)) for k, v in item.items()]
            if all(
                k is old_k and v is old_v
                for (k, v), (old_k, old_v) in zip(pairs, item.items(), strict=True)
            ):
                return item
            return dict(pairs)
        if isinstance(item, (list, tuple)):
            values = [scrub(v) for v in item]
            if all(v is old for v, old in zip(values, item, strict=True)):
                return item
            return tuple(values) if isinstance(item, tuple) else values
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            # An all-digit secret can come back as a JSON number.
            text = str(item)
            return _REDACTION if any(secret in text for secret in configured) else item
        if item is None or isinstance(item, bool):
            return item
        rendered = str(item)
        scrubbed = scrub(rendered)
        return scrubbed if scrubbed != rendered else item

    if isinstance(value, (dict, list, tuple)):
        rendered = json.dumps(value, ensure_ascii=False, default=str)
        if not any(
            json.dumps(secret, ensure_ascii=False)[1:-1] in rendered for secret in configured
        ):
            return value
    return scrub(value)


class RedactionFilter(logging.Filter):
    """Replace occurrences of a secret in log messages with a placeholder."""

    def __init__(self, secrets: str | list[str | None] | None) -> None:
        super().__init__()
        if isinstance(secrets, str) or secrets is None:
            secrets = [secrets] if secrets else []
        # Only redact non-trivial secrets; empty/very short values would match
        # everywhere and are not real keys.
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            # Render the message (applying args) then scrub, so interpolated
            # secrets are caught too.
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            scrubbed = redact(message, self._secrets)
            if scrubbed != message:
                record.msg = scrubbed
                record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Scrub secrets from the fully formatted output, tracebacks included.

    Filters never see exception text — ``exc_info`` is rendered at format
    time — so a secret inside an exception message would bypass
    :class:`RedactionFilter`. Scrubbing the final string closes that gap.
    """

    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact(formatted, self._secrets)


def configure_logging(
    level: str = "INFO",
    api_key: str | None = None,
    secrets: list[str] | None = None,
) -> None:
    """Configure root logging to stderr with secret redaction.

    ``api_key`` plus any extra ``secrets`` are scrubbed from every record.
    Idempotent: replaces any handlers we previously installed.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Stateless streamable-HTTP tears down its per-request transport after
    # every call, and the SDK logs that at INFO ("Terminating session: None") —
    # one meaningless line per request. Keep that logger at WARNING.
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)

    # websockets logs raw frames at DEBUG (and its own repr/escaping), which
    # can embed secrets in forms literal-substring redaction cannot match.
    # Never emit them, whatever our own level is.
    logging.getLogger("websockets").setLevel(logging.INFO)

    # Remove handlers we control to keep this idempotent across reconfigures.
    for handler in list(root.handlers):
        if getattr(handler, "_unraid_mcp", False):
            root.removeHandler(handler)

    all_secrets: list[str | None] = [api_key, *(secrets or [])]
    handler = logging.StreamHandler(stream=sys.stderr)
    handler._unraid_mcp = True  # type: ignore[attr-defined]
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            [s for s in all_secrets if s],
        )
    )
    handler.addFilter(RedactionFilter(all_secrets))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
