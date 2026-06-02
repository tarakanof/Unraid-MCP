"""Tests for settings/config loading and validation."""

from __future__ import annotations

import pytest

from unraid_mcp.config import load_settings
from unraid_mcp.errors import UnraidConfigError

REQUIRED = {"UNRAID_API_URL": "https://tower.local/graphql", "UNRAID_API_KEY": "supersecretkey123"}
TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
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
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_tls_enabled_requires_both_cert_and_key(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    assert load_settings(_env_file=None).tls_enabled is False
    clean_env.setenv("UNRAID_MCP_TLS_CERT", "/etc/ssl/cert.pem")
    assert load_settings(_env_file=None).tls_enabled is False  # key still missing
    clean_env.setenv("UNRAID_MCP_TLS_KEY", "/etc/ssl/key.pem")
    assert load_settings(_env_file=None).tls_enabled is True


def test_missing_required_raises_config_error(clean_env):
    with pytest.raises(UnraidConfigError) as exc:
        load_settings(_env_file=None)
    msg = str(exc.value)
    assert "UNRAID_API_URL" in msg
    assert "UNRAID_API_KEY" in msg


def test_defaults(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    s = load_settings(_env_file=None)
    assert s.transport == "stdio"
    assert s.host == "127.0.0.1"
    assert s.port == 6750
    assert s.verify_ssl is True
    assert s.allow_mutations is False
    assert s.allow_raw_query is False
    assert s.timeout == 30.0
    assert s.log_level == "INFO"
    assert s.ca_bundle is None


def test_url_normalization_appends_graphql(clean_env):
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_API_URL", "https://tower.local")
    assert load_settings(_env_file=None).api_url == "https://tower.local/graphql"


def test_url_normalization_trailing_slash(clean_env):
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_API_URL", "https://tower.local/")
    assert load_settings(_env_file=None).api_url == "https://tower.local/graphql"


def test_url_with_path_left_intact(clean_env):
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_API_URL", "http://10.0.0.5:8080/graphql")
    assert load_settings(_env_file=None).api_url == "http://10.0.0.5:8080/graphql"


def test_invalid_scheme_rejected_without_leaking_secret(clean_env):
    clean_env.setenv("UNRAID_API_KEY", "supersecretkey123")
    clean_env.setenv("UNRAID_API_URL", "ftp://tower.local")
    with pytest.raises(UnraidConfigError) as exc:
        load_settings(_env_file=None)
    assert "supersecretkey123" not in str(exc.value)


def test_api_key_is_secret_and_not_in_repr(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    s = load_settings(_env_file=None)
    assert s.api_key.get_secret_value() == "supersecretkey123"
    assert "supersecretkey123" not in repr(s)
    assert "supersecretkey123" not in str(s)


def test_blank_bearer_token_is_treated_as_unset(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("UNRAID_MCP_BEARER_TOKEN", "")
    assert load_settings(_env_file=None).bearer_token is None


def test_valid_bearer_token_is_secret(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("UNRAID_MCP_BEARER_TOKEN", TOKEN)
    s = load_settings(_env_file=None)
    assert s.bearer_token is not None
    assert s.bearer_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(s)


@pytest.mark.parametrize(
    "token",
    [
        "short-token",
        "change-me-to-a-long-random-token",
        " replace-me-with-a-32-character-token ",
    ],
)
def test_insecure_bearer_token_rejected_without_leaking_value(clean_env, token):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("UNRAID_MCP_BEARER_TOKEN", token)
    with pytest.raises(UnraidConfigError) as exc:
        load_settings(_env_file=None)
    msg = str(exc.value)
    assert "UNRAID_MCP_BEARER_TOKEN" in msg
    assert token not in msg


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False)],
)
def test_bool_parsing(clean_env, raw, expected):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("UNRAID_MCP_ALLOW_MUTATIONS", raw)
    assert load_settings(_env_file=None).allow_mutations is expected


def test_port_and_timeout_coerced_from_strings(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("UNRAID_MCP_PORT", "7000")
    clean_env.setenv("UNRAID_MCP_TIMEOUT", "12.5")
    s = load_settings(_env_file=None)
    assert s.port == 7000
    assert s.timeout == 12.5
