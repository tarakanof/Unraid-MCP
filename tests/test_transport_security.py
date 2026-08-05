"""The DNS-rebinding behaviour matrix for the streamable-HTTP transport.

SDK v2 auto-enables its own localhost allow-list when an app is built with
``transport_security=None`` and a localhost ``host``. These tests pin our own
matrix on top of it: localhost -> protected with an allow-list that includes the
configured port; non-localhost + allow-list -> protected with that list;
non-localhost without an allow-list -> off (the bearer token guards access, and
``cli`` warns), which also proves the SDK default cannot fire behind our back.
"""

from __future__ import annotations

from unraid_mcp.server import _transport_security, build_server, http_app

from .conftest import make_settings

HTTP = {"transport": "streamable-http"}


def test_localhost_bind_is_protected_including_the_configured_port():
    settings = make_settings(**HTTP, port=6750)
    security = _transport_security(settings)
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1:6750" in security.allowed_hosts
    assert "http://127.0.0.1:6750" in security.allowed_origins


def test_non_localhost_with_allow_list_is_protected_with_that_list():
    settings = make_settings(**HTTP, host="0.0.0.0", allowed_hosts="tower.example:6750")
    security = _transport_security(settings)
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert "tower.example:6750" in security.allowed_hosts


def test_non_localhost_without_allow_list_leaves_protection_off():
    settings = make_settings(**HTTP, host="0.0.0.0")
    assert _transport_security(settings) is None


def test_stdio_transport_needs_no_transport_security():
    assert _transport_security(make_settings()) is None


def test_http_app_does_not_inherit_the_sdk_localhost_default(settings_factory):
    """A non-localhost bind must reach the session manager with protection off.

    The SDK enables its localhost allow-list whenever it is handed
    ``transport_security=None`` *and* a localhost host, so ``http_app`` passes the
    real bind address through.
    """
    settings = settings_factory(**HTTP, host="0.0.0.0")
    mcp = build_server(settings)
    http_app(mcp, settings)
    assert mcp.session_manager.security_settings is None


def test_http_app_applies_our_localhost_settings(settings_factory):
    settings = settings_factory(**HTTP, port=6751)
    mcp = build_server(settings)
    http_app(mcp, settings)
    security = mcp.session_manager.security_settings
    assert security is not None
    assert "127.0.0.1:6751" in security.allowed_hosts
