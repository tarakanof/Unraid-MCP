"""Container healthcheck probe: URL selection and exit codes."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request

import pytest

from unraid_mcp import healthcheck
from unraid_mcp.config import BindSettings
from unraid_mcp.healthcheck import health_url, probe


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for k in ("HOST", "PORT", "TLS_CERT", "TLS_KEY"):
        monkeypatch.delenv(f"UNRAID_MCP_{k}", raising=False)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "http://127.0.0.1:6750/health"),
        ({"UNRAID_MCP_HOST": "0.0.0.0"}, "http://127.0.0.1:6750/health"),
        ({"UNRAID_MCP_HOST": "::", "UNRAID_MCP_PORT": "9000"}, "http://127.0.0.1:9000/health"),
        ({"UNRAID_MCP_HOST": "::1"}, "http://[::1]:6750/health"),
        ({"UNRAID_MCP_HOST": "10.0.0.5"}, "http://10.0.0.5:6750/health"),
        (
            {"UNRAID_MCP_HOST": "0.0.0.0", "UNRAID_MCP_TLS_CERT": "c", "UNRAID_MCP_TLS_KEY": "k"},
            "https://127.0.0.1:6750/health",
        ),
        ({"UNRAID_MCP_TLS_CERT": "c"}, "http://127.0.0.1:6750/health"),
        # Same case-insensitive semantics as the server's Settings.
        (
            {"unraid_mcp_tls_cert": "c", "unraid_mcp_tls_key": "k", "unraid_mcp_port": "7000"},
            "https://127.0.0.1:7000/health",
        ),
    ],
)
def test_health_url(monkeypatch, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert health_url(BindSettings()) == expected


def test_reads_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("UNRAID_MCP_PORT=7100\n")
    assert health_url(BindSettings()) == "http://127.0.0.1:7100/health"


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_opener(monkeypatch, result):
    seen = {}
    real = urllib.request.build_opener

    def fake_build(*handlers):
        seen["handlers"] = handlers
        opener = real(*handlers)

        def open_(url, timeout=None):
            seen["url"] = url
            if isinstance(result, Exception):
                raise result
            return result

        opener.open = open_
        return opener

    monkeypatch.setattr(healthcheck.urllib.request, "build_opener", fake_build)
    return seen


def test_probe_ok_http(monkeypatch):
    seen = _patch_opener(monkeypatch, _Resp(200))
    assert probe() == 0
    assert seen["url"] == "http://127.0.0.1:6750/health"
    assert not any(isinstance(h, urllib.request.HTTPSHandler) for h in seen["handlers"])


def test_probe_tls_skips_verification(monkeypatch):
    monkeypatch.setenv("UNRAID_MCP_TLS_CERT", "c")
    monkeypatch.setenv("UNRAID_MCP_TLS_KEY", "k")
    seen = _patch_opener(monkeypatch, _Resp(200))
    assert probe() == 0
    assert seen["url"].startswith("https://")
    https = next(h for h in seen["handlers"] if isinstance(h, urllib.request.HTTPSHandler))
    assert https._context.verify_mode == ssl.CERT_NONE
    assert https._context.check_hostname is False


def test_probe_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    seen = _patch_opener(monkeypatch, _Resp(200))
    assert probe() == 0
    proxies = next(h for h in seen["handlers"] if isinstance(h, urllib.request.ProxyHandler))
    assert proxies.proxies == {}


def test_probe_non_200_is_unhealthy(monkeypatch):
    _patch_opener(monkeypatch, _Resp(503))
    assert probe() == 1


def test_probe_error_is_unhealthy(monkeypatch):
    _patch_opener(monkeypatch, urllib.error.URLError("refused"))
    assert probe() == 1


def test_probe_bad_config_is_unhealthy(monkeypatch):
    monkeypatch.setenv("UNRAID_MCP_PORT", "notaport")
    assert probe() == 1
