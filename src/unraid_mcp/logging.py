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

    # Remove handlers we control to keep this idempotent across reconfigures.
    for handler in list(root.handlers):
        if getattr(handler, "_unraid_mcp", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler._unraid_mcp = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactionFilter([api_key, *(secrets or [])]))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
