"""Tests for the CLI entry point wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from unraid_mcp import cli

ENV_VARS = (
    "UNRAID_API_URL",
    "UNRAID_API_KEY",
    "UNRAID_VERIFY_SSL",
    "UNRAID_CA_BUNDLE",
    "UNRAID_MCP_TRANSPORT",
    "UNRAID_MCP_HOST",
    "UNRAID_MCP_PORT",
    "UNRAID_MCP_BEARER_TOKEN",
    "UNRAID_MCP_ALLOW_MUTATIONS",
    "UNRAID_MCP_ALLOW_RAW_QUERY",
    "UNRAID_MCP_TIMEOUT",
    "UNRAID_MCP_LOG_LEVEL",
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_main_returns_1_on_missing_config(clean_env):
    assert cli.main() == 1


def test_main_runs_stdio_transport(clean_env, monkeypatch):
    clean_env.setenv("UNRAID_API_URL", "https://tower.local/graphql")
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    fake = MagicMock()
    monkeypatch.setattr(cli, "build_server", lambda settings: fake)
    assert cli.main() == 0
    fake.run.assert_called_once_with(transport="stdio")


def test_main_http_transport_serves_with_auth(clean_env, monkeypatch):
    clean_env.setenv("UNRAID_API_URL", "https://tower.local/graphql")
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_MCP_TRANSPORT", "streamable-http")
    clean_env.setenv("UNRAID_MCP_BEARER_TOKEN", "client-token-123456")
    monkeypatch.setattr(cli, "build_server", lambda settings: MagicMock())
    served = {}
    monkeypatch.setattr(cli, "_serve_http", lambda mcp, settings: served.update(host=settings.host))
    assert cli.main() == 0
    assert served["host"] == "127.0.0.1"


def _capture_serve_http(monkeypatch):
    import uvicorn

    captured = {}

    def rec(app, token):
        captured["token"] = token
        return app

    monkeypatch.setattr(cli, "StaticBearerAuthMiddleware", rec)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    return captured


def test_serve_http_generates_token_when_absent(settings_factory, monkeypatch):
    captured = _capture_serve_http(monkeypatch)
    cli._serve_http(MagicMock(), settings_factory(transport="streamable-http"))
    assert len(captured["token"]) >= 20  # a generated random token


def test_serve_http_uses_provided_token(settings_factory, monkeypatch):
    captured = _capture_serve_http(monkeypatch)
    cli._serve_http(
        MagicMock(),
        settings_factory(transport="streamable-http", bearer_token="my-fixed-token-123456"),
    )
    assert captured["token"] == "my-fixed-token-123456"
