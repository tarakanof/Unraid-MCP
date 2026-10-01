"""Container liveness probe: ``python -m unraid_mcp.healthcheck``.

Resolves only the bind settings (``UNRAID_MCP_HOST``/``PORT``/``TLS_CERT``/
``TLS_KEY``) via :class:`BindSettings` (same env/.env semantics as the server),
so it needs no Unraid API key or bearer token and never calls the Unraid API.
Probes ``/health`` on the bound interface (loopback for wildcard hosts) over
https when the server serves TLS directly, skipping certificate verification
for this probe only. Ignores HTTP(S)_PROXY. Exits 0 if healthy.
"""

from __future__ import annotations

import ssl
import sys
import urllib.request

from .config import BindSettings

_WILDCARD_HOSTS = ("", "0.0.0.0", "::")  # noqa: S104  # nosec B104 - probe targets loopback


def health_url(settings: BindSettings) -> str:
    """``/health`` URL matching how the server is bound (wildcard -> loopback)."""
    host = settings.host.strip()
    if host in _WILDCARD_HOSTS:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):  # bare IPv6 literal
        host = f"[{host}]"
    scheme = "https" if settings.tls_enabled else "http"
    return f"{scheme}://{host}:{settings.port}/health"


def probe(settings: BindSettings | None = None, timeout: float = 3.0) -> int:
    """Return 0 if ``/health`` answers 200, else 1."""
    try:
        settings = settings or BindSettings()
        url = health_url(settings)
        handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
        if url.startswith("https://"):
            # Self-signed certs are common; skipped for this local probe only.
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(url, timeout=timeout) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:
        return 1


def main() -> int:
    return probe()


if __name__ == "__main__":
    sys.exit(main())
