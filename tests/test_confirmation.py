"""Human confirmation through both MCP transports, with a mocked Unraid API."""

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.types import ElicitResult, InputRequiredResult

from unraid_mcp.server import build_server

URL = "https://tower.local/graphql"

# A successful result for every mutation root the destructive tools can hit.
OK_DATA = {
    "array": {
        "setState": True,
        "mountArrayDisk": True,
        "unmountArrayDisk": True,
        "clearArrayDiskStatistics": True,
        "addDiskToArray": True,
    },
    "docker": {
        "stop": True,
        "restart": True,
        "updateContainer": True,
        "updateContainers": [{"id": "c1"}],
        "removeContainer": True,
        "updateAllContainers": [{"id": "c1"}],
    },
    "vm": {"stop": True, "reboot": True, "forceStop": True, "reset": True},
    "archiveAll": True,
    "deleteNotification": True,
    "deleteArchivedNotifications": True,
}

# Every destructive tool, including all dangerous-tier tools. Dynamic consequences
# are checked against the existing confirm-only refusal, not a second string list.
DESTRUCTIVE_CALLS = [
    ("stop_array", {}),
    ("mount_array_disk", {"disk_id": "disk1"}),
    ("unmount_array_disk", {"disk_id": "disk1"}),
    ("clear_disk_statistics", {"disk_id": "disk1"}),
    ("add_disk_to_array", {"disk_id": "disk1", "slot": 2}),
    ("stop_docker_container", {"container_id": "container1"}),
    ("restart_docker_container", {"container_id": "container1"}),
    ("update_docker_container", {"container_id": "container1"}),
    ("update_docker_containers", {"container_ids": ["container1", "container2"]}),
    ("remove_docker_container", {"container_id": "container1"}),
    ("remove_docker_container", {"container_id": "container1", "with_image": True}),
    ("update_all_docker_containers", {}),
    ("stop_vm", {"vm_id": "vm1"}),
    ("reboot_vm", {"vm_id": "vm1"}),
    ("force_stop_vm", {"vm_id": "vm1"}),
    ("reset_vm", {"vm_id": "vm1"}),
    ("archive_all_notifications", {}),
    ("archive_all_notifications", {"importance": "ALERT"}),
    ("delete_notification", {"notification_id": "note1", "notification_type": "UNREAD"}),
    ("delete_archived_notifications", {}),
]


def error_text(result):
    assert result.is_error
    return " ".join(block.text for block in result.content if block.type == "text")


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("action", ["accept", "decline", "cancel", "accept_false"])
@pytest.mark.parametrize("tool,arguments", DESTRUCTIVE_CALLS, ids=[c[0] for c in DESTRUCTIVE_CALLS])
async def test_destructive_confirmation(settings_factory, mode, action, tool, arguments):
    messages = []

    async def elicit(context, params):
        # No mutation has been sent while the human is being asked.
        assert len(respx.calls) == 0
        messages.append(params.message)
        return ElicitResult(
            action="accept" if action == "accept_false" else action,
            content={"proceed": action == "accept"} if action.startswith("accept") else None,
        )

    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        mcp = build_server(settings_factory(allow_mutations=True, allow_dangerous=True))
        async with Client(mcp, mode=mode, elicitation_callback=elicit) as client:
            # The lifespan probes the API once at startup. Only calls made by the
            # tool under test count toward the no-I/O invariant.
            respx.calls.clear()
            route.calls.clear()
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools if t.annotations.destructive_hint} == {
                c[0] for c in DESTRUCTIVE_CALLS
            }
            schema = next(t.input_schema for t in tools if t.name == tool)
            assert "confirmation" not in schema["properties"]
            refused = await client.call_tool(tool, arguments)
            refusal = error_text(refused)
            assert "confirm=true" in refusal
            assert not messages
            assert len(respx.calls) == 0

            result = await client.call_tool(tool, {**arguments, "confirm": True})
            consequence = refusal.split("Refusing to ", 1)[1].split(
                " without explicit confirmation."
            )[0]
            assert messages == [consequence]
            if action == "accept":
                assert not result.is_error
                assert route.call_count >= 1
            else:
                assert "cancelled by user" in error_text(result)
                assert len(respx.calls) == 0


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_confirm_only_fallback_and_non_destructive_tools(settings_factory, mode):
    async def unexpected_elicit(context, params):
        pytest.fail("Non-destructive tools must not elicit")

    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, mode=mode) as client:
            respx.calls.clear()
            route.calls.clear()
            assert "confirm=true" in error_text(await client.call_tool("stop_array", {}))
            assert len(respx.calls) == 0
            assert not (await client.call_tool("stop_array", {"confirm": True})).is_error
            assert route.call_count == 1
        async with Client(mcp, mode=mode, elicitation_callback=unexpected_elicit) as client:
            respx.calls.clear()
            route.calls.clear()
            assert "confirm=true" in error_text(await client.call_tool("start_array", {}))
            assert len(respx.calls) == 0
            assert not (await client.call_tool("start_array", {"confirm": True})).is_error
            assert route.call_count == 1


async def test_modern_confirmation_returns_input_required_before_io(settings_factory):
    async def elicit(context, params):
        return ElicitResult(action="accept", content={"proceed": True})

    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, elicitation_callback=elicit) as client:
            respx.calls.clear()
            result = await client.session.call_tool(
                "stop_array", {"confirm": True}, allow_input_required=True
            )
            assert isinstance(result, InputRequiredResult)
            assert len(result.input_requests) == 1
            assert len(respx.calls) == 0


@pytest.mark.parametrize("data", [None, {"array": {"setState": None}}])
async def test_accepted_confirmation_missing_result(settings_factory, data):
    async def elicit(context, params):
        return ElicitResult(action="accept", content={"proceed": True})

    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": data}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, elicitation_callback=elicit) as client:
            route.calls.clear()
            result = await client.call_tool("stop_array", {"confirm": True})
            # main validates mutation envelopes: an empty result is a clean tool error,
            # raised after the accepted prompt reached the backend exactly once.
            assert "missing a non-null result" in error_text(result)
            assert route.call_count == 1


async def test_accepted_confirmation_maps_backend_errors(settings_factory):
    async def elicit(context, params):
        return ElicitResult(action="accept", content={"proceed": True})

    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, elicitation_callback=elicit) as client:
            route.mock(return_value=httpx.Response(200, json={"errors": [{"message": "Denied"}]}))
            result = await client.call_tool("stop_array", {"confirm": True})
            assert "Denied" in error_text(result)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("content", [None, {}, {"proceed": "invalid"}])
async def test_invalid_human_response_refuses_without_io(settings_factory, mode, content):
    async def elicit(context, params):
        return ElicitResult(action="accept", content=content)

    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, mode=mode, elicitation_callback=elicit) as client:
            respx.calls.clear()
            result = await client.call_tool("stop_array", {"confirm": True})
            assert result.is_error
            assert len(respx.calls) == 0


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("tool,arguments", DESTRUCTIVE_CALLS, ids=[c[0] for c in DESTRUCTIVE_CALLS])
async def test_all_destructive_tools_without_elicitation(settings_factory, mode, tool, arguments):
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        mcp = build_server(settings_factory(allow_mutations=True, allow_dangerous=True))
        async with Client(mcp, mode=mode) as client:
            respx.calls.clear()
            route.calls.clear()
            assert "confirm=true" in error_text(await client.call_tool(tool, arguments))
            assert len(respx.calls) == 0
            assert not (await client.call_tool(tool, {**arguments, "confirm": True})).is_error
            assert route.call_count >= 1


@pytest.mark.parametrize(
    "capability,supports_form",
    [(None, False), ({}, True), ({"form": {}}, True), ({"url": {}}, False)],
)
@pytest.mark.parametrize(
    "version,can_send,delivers",
    [
        ("2026-07-28", False, True),  # MRTR path, no back channel needed
        ("2025-11-25", True, True),  # legacy with a live back channel (stdio)
        ("2025-11-25", False, False),  # legacy stateless HTTP: confirm-only
    ],
)
def test_confirmation_capability_modes(capability, supports_form, version, can_send, delivers):
    from types import SimpleNamespace

    from mcp.server.mcpserver import Elicit
    from mcp.types import ClientCapabilities

    from unraid_mcp.tools._base import Confirmation, require_confirmation

    ctx = SimpleNamespace(
        client_capabilities=ClientCapabilities(elicitation=capability),
        protocol_version=version,
        request_context=SimpleNamespace(session=SimpleNamespace(can_send_request=can_send)),
    )
    result = require_confirmation(ctx, True, "stop storage")
    if supports_form and delivers:
        assert isinstance(result, Elicit)
        assert result.message == "stop storage"
    else:
        assert isinstance(result, Confirmation)
        assert result.proceed is True


@pytest.mark.parametrize("mode,elicits", [("legacy", False), ("auto", True)])
async def test_stateless_http_confirmation(settings_factory, mode, elicits):
    """Legacy clients over stateless HTTP have no back channel: confirm-only, and
    the destructive tool must stay usable. Modern clients still elicit (MRTR)."""
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    from unraid_mcp.server import http_app

    messages = []

    async def elicit(context, params):
        assert len(respx.calls) == 0
        messages.append(params.message)
        return ElicitResult(action="accept", content={"proceed": True})

    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        settings = settings_factory(allow_mutations=True, transport="streamable-http", port=8000)
        app = http_app(build_server(settings), settings)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://localhost:8000"
            ) as http:
                stream = streamable_http_client("http://localhost:8000/mcp", http_client=http)
                async with Client(stream, mode=mode, elicitation_callback=elicit) as client:
                    respx.calls.clear()
                    route.calls.clear()
                    assert "confirm=true" in error_text(await client.call_tool("stop_array", {}))
                    assert len(respx.calls) == 0
                    result = await client.call_tool("stop_array", {"confirm": True})
                    assert not result.is_error
                    assert route.call_count == 1
                    assert bool(messages) is elicits


@pytest.mark.parametrize("ids", [[], [f"c{i}" for i in range(21)]])
async def test_invalid_batch_refused_before_prompt(settings_factory, ids):
    async def elicit(context, params):
        pytest.fail("Invalid input must be rejected before prompting the human")

    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": OK_DATA}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, elicitation_callback=elicit) as client:
            respx.calls.clear()
            result = await client.call_tool(
                "update_docker_containers", {"container_ids": ids, "confirm": True}
            )
            assert result.is_error
            assert len(respx.calls) == 0
