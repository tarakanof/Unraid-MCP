"""Logging configuration for the Unraid MCP server.

Two invariants matter here:

1. **Everything goes to stderr.** On the stdio transport, stdout carries the
   JSON-RPC stream, so any stray log line on stdout corrupts the protocol.
2. **The API key is never logged.** A redaction filter scrubs the configured
   secret from every record before it is emitted, as defence in depth on top
   of using ``SecretStr`` everywhere else.
"""

from __future__ import annotations

import logging
import sys

_REDACTION = "***REDACTED***"


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
            scrubbed = message
            for secret in self._secrets:
                if secret in scrubbed:
                    scrubbed = scrubbed.replace(secret, _REDACTION)
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
        for secret in self._secrets:
            if secret in formatted:
                formatted = formatted.replace(secret, _REDACTION)
        return formatted


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
