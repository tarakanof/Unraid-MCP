"""Tests for the CLI entry point wiring."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from unraid_mcp import cli
from unraid_mcp.errors import UnraidConfigError

TOKEN = "0123456789abcdef0123456789abcdef"

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
    "UNRAID_MCP_ALLOWED_HOSTS",
    "UNRAID_MCP_ALLOWED_ORIGINS",
    "UNRAID_MCP_TLS_CERT",
    "UNRAID_MCP_TLS_KEY",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Settings reads a `.env` from the current directory; chdir to an empty
    # tmp dir so a developer's local .env can't leak into these tests.
    monkeypatch.chdir(tmp_path)
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
    clean_env.setenv("UNRAID_MCP_BEARER_TOKEN", TOKEN)
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

    def fake_run(*args, **kwargs):
        captured["uvicorn_kwargs"] = kwargs

    monkeypatch.setattr(cli, "StaticBearerAuthMiddleware", rec)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    return captured


def test_main_generates_token_on_localhost_and_redacts_it(clean_env, monkeypatch, capsys, caplog):
    clean_env.setenv("UNRAID_API_URL", "https://tower.local/graphql")
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_MCP_TRANSPORT", "streamable-http")
    clean_env.setenv("UNRAID_MCP_HOST", "127.0.0.1")
    seen = {}

    def fake_build(settings):
        seen["settings"] = settings
        return MagicMock()

    def fake_serve(mcp, settings):
        logging.getLogger("unraid_mcp.test").warning(
            "later line leaks %s", settings.bearer_token.get_secret_value()
        )

    monkeypatch.setattr(cli, "build_server", fake_build)
    monkeypatch.setattr(cli, "_serve_http", fake_serve)
    with caplog.at_level("DEBUG"):
        assert cli.main() == 0
    token = seen["settings"].bearer_token.get_secret_value()  # reaches build_server/client
    assert len(token) >= 20  # a generated random token
    startup = [r for r in caplog.records if r.name == "unraid_mcp.cli" and token in r.getMessage()]
    assert len(startup) == 1  # the startup line is the only record carrying it
    err = capsys.readouterr().err
    assert token not in err.split("later line")[1]
    assert "later line leaks ***REDACTED***" in err


def test_serve_http_refuses_generated_token_on_non_localhost(settings_factory, monkeypatch, caplog):
    captured = _capture_serve_http(monkeypatch)
    with caplog.at_level("DEBUG"), pytest.raises(UnraidConfigError) as exc:
        cli._serve_http(MagicMock(), settings_factory(transport="streamable-http", host="0.0.0.0"))
    assert "UNRAID_MCP_BEARER_TOKEN is required" in str(exc.value)
    assert "secrets.token_urlsafe(32)" in str(exc.value)
    assert "token" not in captured  # never reached the auth middleware / server
    assert "Bearer <token>" not in caplog.text  # no generated-token log line


def test_main_exits_nonzero_on_non_localhost_without_token(clean_env, monkeypatch, capsys):
    clean_env.setenv("UNRAID_API_URL", "https://tower.local/graphql")
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_MCP_TRANSPORT", "streamable-http")
    clean_env.setenv("UNRAID_MCP_HOST", "0.0.0.0")
    monkeypatch.setattr(cli, "build_server", lambda settings: MagicMock())
    assert cli.main() == 1
    err = capsys.readouterr().err
    assert "UNRAID_MCP_BEARER_TOKEN is required" in err
    assert "Bearer <token>" not in err
    assert "generated one" not in err


def test_serve_http_never_logs_configured_token(settings_factory, monkeypatch, caplog):
    _capture_serve_http(monkeypatch)
    with caplog.at_level("DEBUG"):
        cli._serve_http(
            MagicMock(),
            settings_factory(transport="streamable-http", host="0.0.0.0", bearer_token=TOKEN),
        )
    assert TOKEN not in caplog.text


def test_serve_http_uses_provided_token(settings_factory, monkeypatch):
    captured = _capture_serve_http(monkeypatch)
    cli._serve_http(
        MagicMock(),
        settings_factory(transport="streamable-http", bearer_token=TOKEN),
    )
    assert captured["token"] == TOKEN
    assert "ssl_certfile" not in captured["uvicorn_kwargs"]  # plaintext when no TLS configured


def test_serve_http_enables_tls_when_cert_and_key_set(settings_factory, monkeypatch, tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("x")
    key.write_text("y")
    captured = _capture_serve_http(monkeypatch)
    cli._serve_http(
        MagicMock(),
        settings_factory(
            transport="streamable-http",
            bearer_token=TOKEN,
            tls_cert=str(cert),
            tls_key=str(key),
        ),
    )
    assert captured["uvicorn_kwargs"]["ssl_certfile"] == str(cert)
    assert captured["uvicorn_kwargs"]["ssl_keyfile"] == str(key)


def test_main_stdio_logging_redacts_bearer_token(clean_env, monkeypatch, capsys):
    import logging

    clean_env.setenv("UNRAID_API_URL", "https://tower.local/graphql")
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_MCP_BEARER_TOKEN", TOKEN)
    fake = MagicMock()
    fake.run.side_effect = lambda transport: logging.getLogger("httpcore.transport").debug(
        "< TEXT %r", f'{{"type":"error","payload":"{TOKEN}"}}'
    )
    clean_env.setenv("UNRAID_MCP_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(cli, "build_server", lambda settings: fake)
    assert cli.main() == 0
    err = capsys.readouterr().err
    assert "httpcore.transport" in err
    assert TOKEN not in err
    assert "***REDACTED***" in err


def test_main_http_generated_token_reaches_server_settings(clean_env, monkeypatch, capsys):
    clean_env.setenv("UNRAID_API_URL", "https://tower.local/graphql")
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_MCP_TRANSPORT", "streamable-http")
    built = {}
    monkeypatch.setattr(
        cli, "build_server", lambda settings: built.update(s=settings) or MagicMock()
    )
    served = {}
    monkeypatch.setattr(cli, "_serve_http", lambda mcp, settings: served.update(s=settings))
    assert cli.main() == 0
    token = built["s"].bearer_token.get_secret_value()
    assert len(token) >= 20
    assert served["s"].bearer_token.get_secret_value() == token
    # Shown once at startup, then redacted from later logs.
    import logging

    logging.getLogger("x").warning("leak %s", token)
    assert capsys.readouterr().err.count(token) == 1
