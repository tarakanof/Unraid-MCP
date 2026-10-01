"""Tests for mutation gating: registration behind a flag + confirm requirement."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp import queries
from unraid_mcp.errors import UnraidGraphQLError, UnraidServerError
from unraid_mcp.server import build_server
from unraid_mcp.tools import array, docker, notifications, vm
from unraid_mcp.tools._base import guarded

READ_TOOLS = {"get_system_info", "get_array_status", "list_docker_containers", "get_health_summary"}
# The complete set of mutating tools — kept exhaustive so the registration
# tests catch any tool that fails to register or leaks into read-only mode.
MUTATION_TOOLS = {
    "start_array",
    "stop_array",
    "start_parity_check",
    "pause_parity_check",
    "resume_parity_check",
    "cancel_parity_check",
    "start_docker_container",
    "stop_docker_container",
    "restart_docker_container",
    "pause_docker_container",
    "unpause_docker_container",
    "update_docker_container",
    "update_docker_containers",
    "start_vm",
    "stop_vm",
    "pause_vm",
    "resume_vm",
    "reboot_vm",
    "force_stop_vm",
    "reset_vm",
    "archive_notification",
    "archive_all_notifications",
    "mark_notification_unread",
    "delete_notification",
    "archive_notifications",
    "unarchive_notifications",
    "unarchive_all_notifications",
    "delete_archived_notifications",
    "create_notification",
}
# Dangerous-tier tools — registered only when allow_mutations AND allow_dangerous.
DANGEROUS_TOOLS = {
    "mount_array_disk",
    "unmount_array_disk",
    "clear_disk_statistics",
    "add_disk_to_array",
    "remove_docker_container",
    "update_all_docker_containers",
}


async def _tool_names(settings) -> set[str]:
    mcp = build_server(settings)
    return {t.name for t in await mcp.list_tools()}


async def test_mutations_absent_by_default(settings_factory):
    names = await _tool_names(settings_factory(allow_mutations=False))
    assert names >= READ_TOOLS
    assert names.isdisjoint(MUTATION_TOOLS)


async def test_mutations_present_when_enabled(settings_factory):
    read_only = await _tool_names(settings_factory(allow_mutations=False))
    with_mutations = await _tool_names(settings_factory(allow_mutations=True))
    # Exactly the mutation set appears, nothing more, nothing fewer.
    assert with_mutations - read_only == MUTATION_TOOLS


# ── Dangerous-tier registration matrix ──────────────────────────────────────


async def test_dangerous_absent_with_neither_flag(settings_factory):
    names = await _tool_names(settings_factory(allow_mutations=False, allow_dangerous=False))
    assert names >= READ_TOOLS
    assert names.isdisjoint(DANGEROUS_TOOLS)


async def test_dangerous_absent_with_mutations_only(settings_factory):
    names = await _tool_names(settings_factory(allow_mutations=True, allow_dangerous=False))
    # Normal mutations present, dangerous tier still gated off.
    assert names >= MUTATION_TOOLS
    assert names.isdisjoint(DANGEROUS_TOOLS)


async def test_dangerous_absent_with_dangerous_flag_alone(settings_factory):
    # allow_dangerous without allow_mutations must unlock nothing.
    names = await _tool_names(settings_factory(allow_mutations=False, allow_dangerous=True))
    assert names >= READ_TOOLS
    assert names.isdisjoint(MUTATION_TOOLS)
    assert names.isdisjoint(DANGEROUS_TOOLS)


async def test_dangerous_present_only_when_both_flags(settings_factory):
    mutations_only = await _tool_names(
        settings_factory(allow_mutations=True, allow_dangerous=False)
    )
    both = await _tool_names(settings_factory(allow_mutations=True, allow_dangerous=True))
    # Enabling dangerous adds exactly the dangerous set on top of mutations.
    assert both - mutations_only == DANGEROUS_TOOLS
    assert both >= READ_TOOLS


async def test_correcting_parity_check_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_start_parity(client, correct=True, confirm=False)
        assert route.call_count == 0


async def test_parity_check_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_start_parity(client, correct=False, confirm=False)
        assert route.call_count == 0


async def test_parity_check_with_confirm_sends(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"parityCheck": {"start": []}}})
    ) as (client, route):
        out = await array.do_start_parity(client, correct=False, confirm=True)
        assert out == {"ok": True, "history_count": 0}
        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content)["variables"] == {"correct": False}


async def test_start_array_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_start_array(client, confirm=False)
        assert route.call_count == 0


async def test_start_vm_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await vm.do_start_vm(client, "uuid-1", confirm=False)
        assert route.call_count == 0


async def test_archive_notification_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_archive_notification(client, "n1", confirm=False)
        assert route.call_count == 0


async def test_raw_query_gated(settings_factory):
    assert "run_graphql_query" not in await _tool_names(settings_factory(allow_raw_query=False))
    assert "run_graphql_query" in await _tool_names(settings_factory(allow_raw_query=True))


async def test_stop_array_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_stop_array(client, confirm=False)
        assert route.call_count == 0


async def test_stop_array_with_confirm_sends_mutation(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"array": {"setState": {"state": "STOPPED"}}}})
    ) as (client, route):
        await array.do_stop_array(client, confirm=True)
        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content)["query"] == queries.STOP_ARRAY


async def test_start_container_sends_id_variable(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"docker": {"start": {"id": "1:a"}}}})
    ) as (client, route):
        await docker.do_start_container(client, "1:abc", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.START_CONTAINER
        assert body["variables"] == {"id": "1:abc"}


async def test_stop_container_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_stop_container(client, "1:abc", confirm=False)
        assert route.call_count == 0


async def test_restart_container_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_restart_container(client, "1:abc", confirm=False)
        assert route.call_count == 0


async def test_restart_container_uses_native_mutation(mocked_client):
    resp = httpx.Response(
        200, json={"data": {"docker": {"restart": {"id": "1:abc", "state": "RUNNING"}}}}
    )
    async with mocked_client(resp) as (client, route):
        await docker.do_restart_container(client, "1:abc", confirm=True)
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.RESTART_CONTAINER
        assert body["variables"] == {"id": "1:abc"}


async def test_restart_container_falls_back_on_old_api(mocked_client):
    unsupported = httpx.Response(
        200,
        json={"errors": [{"message": 'Cannot query field "restart" on type "DockerMutations".'}]},
    )
    stop_resp = httpx.Response(200, json={"data": {"docker": {"stop": {"id": "1:abc"}}}})
    start_resp = httpx.Response(200, json={"data": {"docker": {"start": {"id": "1:abc"}}}})
    async with mocked_client([unsupported, stop_resp, start_resp]) as (client, route):
        await docker.do_restart_container(client, "1:abc", confirm=True)
        assert route.call_count == 3
        queries_sent = [json.loads(call.request.content)["query"] for call in route.calls]
        assert queries_sent == [
            queries.RESTART_CONTAINER,
            queries.STOP_CONTAINER,
            queries.START_CONTAINER,
        ]


async def test_pause_container_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_pause_container(client, "1:abc", confirm=False)
        assert route.call_count == 0


async def test_pause_container_sends_id_variable(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"docker": {"pause": {"id": "1:abc"}}}})
    ) as (client, route):
        await docker.do_pause_container(client, "1:abc", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.PAUSE_CONTAINER
        assert body["variables"] == {"id": "1:abc"}


async def test_pause_container_unsupported_api_raises_friendly_error(mocked_client):
    unsupported = httpx.Response(
        200,
        json={"errors": [{"message": 'Cannot query field "pause" on type "DockerMutations".'}]},
    )
    async with mocked_client(unsupported) as (client, route):
        with pytest.raises(ToolError, match="does not support"):
            await docker.do_pause_container(client, "1:abc", confirm=True, api_version="2.100.0")
        assert route.call_count == 1


async def test_unpause_container_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_unpause_container(client, "1:abc", confirm=False)
        assert route.call_count == 0


async def test_unpause_container_sends_id_variable(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"docker": {"unpause": {"id": "1:abc"}}}})
    ) as (client, route):
        await docker.do_unpause_container(client, "1:abc", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.UNPAUSE_CONTAINER
        assert body["variables"] == {"id": "1:abc"}


async def test_unpause_container_unsupported_api_raises_friendly_error(mocked_client):
    unsupported = httpx.Response(
        200,
        json={"errors": [{"message": 'Cannot query field "unpause" on type "DockerMutations".'}]},
    )
    async with mocked_client(unsupported) as (client, route):
        with pytest.raises(ToolError, match="does not support"):
            await docker.do_unpause_container(client, "1:abc", confirm=True)
        assert route.call_count == 1


# ── Docker container updates (pull + recreate) ───────────────────────────────


async def test_update_container_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_update_container(client, "1:abc", confirm=False)
        assert route.call_count == 0


async def test_update_container_with_confirm_sends_and_shapes(mocked_client):
    async with mocked_client(
        httpx.Response(
            200,
            json={
                "data": {
                    "docker": {
                        "updateContainer": {
                            "id": "1:abc",
                            "names": ["/plex"],
                            "state": "RUNNING",
                            "status": "Up 2 seconds",
                        }
                    }
                }
            },
        )
    ) as (client, route):
        result = await docker.do_update_container(client, "1:abc", confirm=True)
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.UPDATE_CONTAINER
        assert body["variables"] == {"id": "1:abc"}
        assert result == {
            "id": "1:abc",
            "names": ["/plex"],
            "state": "RUNNING",
            "status": "Up 2 seconds",
        }


async def test_update_container_rejects_blank_id_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_update_container(client, "   ", confirm=True)
        assert route.call_count == 0


async def test_update_container_old_api_friendly_error(mocked_client):
    async with mocked_client(
        httpx.Response(
            200,
            json={
                "errors": [{"message": 'Cannot query field "updateContainer" on type "Docker".'}],
                "data": None,
            },
        )
    ) as (client, route):
        with pytest.raises(ToolError) as excinfo:
            await docker.do_update_container(client, "1:abc", confirm=True)
        assert "does not support" in str(excinfo.value)


async def test_update_containers_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_update_containers(client, ["1:a", "1:b"], confirm=False)
        assert route.call_count == 0


async def test_update_containers_with_confirm_sends_and_shapes_list(mocked_client):
    async with mocked_client(
        httpx.Response(
            200,
            json={
                "data": {
                    "docker": {
                        "updateContainers": [
                            {"id": "1:a", "names": ["/a"], "state": "RUNNING", "status": "Up"},
                            {"id": "1:b", "names": ["/b"], "state": "RUNNING", "status": "Up"},
                        ]
                    }
                }
            },
        )
    ) as (client, route):
        result = await docker.do_update_containers(client, ["1:a", "1:b"], confirm=True)
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.UPDATE_CONTAINERS
        assert body["variables"] == {"ids": ["1:a", "1:b"]}
        assert isinstance(result, list)
        assert [c["id"] for c in result] == ["1:a", "1:b"]


async def test_update_containers_rejects_empty_list_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_update_containers(client, [], confirm=True)
        assert route.call_count == 0


async def test_update_containers_rejects_over_cap_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        ids = [f"1:{i}" for i in range(docker.MAX_UPDATE_CONTAINERS + 1)]
        with pytest.raises(ToolError):
            await docker.do_update_containers(client, ids, confirm=True)
        assert route.call_count == 0


async def test_update_all_containers_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_update_all_containers(client, confirm=False)
        assert route.call_count == 0


async def test_update_all_containers_with_confirm_sends_and_shapes_list(mocked_client):
    async with mocked_client(
        httpx.Response(
            200,
            json={
                "data": {
                    "docker": {
                        "updateAllContainers": [
                            {"id": "1:a", "names": ["/a"], "state": "RUNNING", "status": "Up"}
                        ]
                    }
                }
            },
        )
    ) as (client, route):
        result = await docker.do_update_all_containers(client, confirm=True)
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.UPDATE_ALL_CONTAINERS
        assert [c["id"] for c in result] == ["1:a"]


async def test_update_all_containers_old_api_friendly_error(mocked_client):
    async with mocked_client(
        httpx.Response(
            200,
            json={
                "errors": [
                    {"message": 'Cannot query field "updateAllContainers" on type "Docker".'}
                ],
                "data": None,
            },
        )
    ) as (client, route):
        with pytest.raises(ToolError) as excinfo:
            await docker.do_update_all_containers(client, confirm=True)
        assert "does not support" in str(excinfo.value)


async def test_force_stop_vm_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await vm.do_force_stop_vm(client, "uuid-1", confirm=False)
        assert route.call_count == 0


async def test_reset_vm_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await vm.do_reset_vm(client, "uuid-1", confirm=False)
        assert route.call_count == 0


async def test_reset_vm_with_confirm_sends(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {"vm": {"reset": True}}})) as (
        client,
        route,
    ):
        result = await vm.do_reset_vm(client, "uuid-1", confirm=True)
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.VM_RESET
        assert body["variables"] == {"id": "uuid-1"}
        assert result == {"ok": True}


async def test_reset_vm_propagates_graphql_error(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"errors": [{"message": "vm not found"}], "data": None})
    ) as (client, route):
        with pytest.raises(UnraidGraphQLError):
            await vm.do_reset_vm(client, "uuid-1", confirm=True)


async def test_delete_notification_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_delete_notification(client, "n1", "ARCHIVE", confirm=False)
        assert route.call_count == 0


async def test_delete_notification_with_confirm_sends_type(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"deleteNotification": _OVERVIEW_PAYLOAD}})
    ) as (client, route):
        await notifications.do_delete_notification(client, "n1", "ARCHIVE", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["variables"] == {"id": "n1", "type": "ARCHIVE"}


# ── Notification lifecycle bulk ops (#24) ────────────────────────────────────

_OVERVIEW_PAYLOAD = {
    "unread": {"info": 1, "warning": 0, "alert": 0, "total": 1},
    "archive": {"info": 2, "warning": 1, "alert": 0, "total": 3},
}


async def test_archive_notifications_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_archive_notifications(client, ["n1"], confirm=False)
        assert route.call_count == 0


async def test_archive_notifications_rejects_empty_ids_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_archive_notifications(client, [], confirm=True)
        assert route.call_count == 0


async def test_archive_notifications_with_confirm_sends_ids(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"archiveNotifications": _OVERVIEW_PAYLOAD}})
    ) as (client, route):
        result = await notifications.do_archive_notifications(client, ["n1", "n2"], confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.ARCHIVE_NOTIFICATIONS
        assert body["variables"] == {"ids": ["n1", "n2"]}
        assert result == _OVERVIEW_PAYLOAD


async def test_unarchive_notifications_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_unarchive_notifications(client, ["n1"], confirm=False)
        assert route.call_count == 0


async def test_unarchive_notifications_rejects_empty_ids_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_unarchive_notifications(client, [], confirm=True)
        assert route.call_count == 0


async def test_unarchive_notifications_with_confirm_sends_ids(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"unarchiveNotifications": _OVERVIEW_PAYLOAD}})
    ) as (client, route):
        result = await notifications.do_unarchive_notifications(client, ["n1"], confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.UNARCHIVE_NOTIFICATIONS
        assert body["variables"] == {"ids": ["n1"]}
        assert result == _OVERVIEW_PAYLOAD


async def test_unarchive_all_notifications_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_unarchive_all(client, None, confirm=False)
        assert route.call_count == 0


async def test_unarchive_all_notifications_rejects_invalid_importance_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_unarchive_all(client, "CRITICAL", confirm=True)
        assert route.call_count == 0


async def test_unarchive_all_notifications_with_confirm_sends_importance(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"unarchiveAll": _OVERVIEW_PAYLOAD}})
    ) as (client, route):
        result = await notifications.do_unarchive_all(client, "WARNING", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.UNARCHIVE_ALL_NOTIFICATIONS
        assert body["variables"] == {"importance": "WARNING"}
        assert result == _OVERVIEW_PAYLOAD


async def test_delete_archived_notifications_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_delete_archived_notifications(client, confirm=False)
        assert route.call_count == 0


async def test_delete_archived_notifications_with_confirm_sends(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"deleteArchivedNotifications": _OVERVIEW_PAYLOAD}})
    ) as (client, route):
        result = await notifications.do_delete_archived_notifications(client, confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.DELETE_ARCHIVED_NOTIFICATIONS
        assert body["variables"] == {}
        assert result == _OVERVIEW_PAYLOAD


async def test_create_notification_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_create_notification(
                client,
                "Backup done",
                "Backup",
                "Nightly backup finished OK.",
                "INFO",
                confirm=False,
            )
        assert route.call_count == 0


async def test_create_notification_rejects_invalid_importance_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_create_notification(
                client,
                "Backup done",
                "Backup",
                "Nightly backup finished OK.",
                "CRITICAL",
                confirm=True,
            )
        assert route.call_count == 0


async def test_create_notification_with_confirm_sends_input(mocked_client):
    payload = {
        "id": "n9",
        "title": "Backup done",
        "subject": "Backup",
        "description": "Nightly backup finished OK.",
        "importance": "INFO",
        "link": None,
        "type": "UNREAD",
        "timestamp": "2026-07-04T00:00:00Z",
    }
    async with mocked_client(
        httpx.Response(200, json={"data": {"createNotification": payload}})
    ) as (client, route):
        result = await notifications.do_create_notification(
            client, "Backup done", "Backup", "Nightly backup finished OK.", "INFO", confirm=True
        )
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.CREATE_NOTIFICATION
        assert body["variables"] == {
            "input": {
                "title": "Backup done",
                "subject": "Backup",
                "description": "Nightly backup finished OK.",
                "importance": "INFO",
            }
        }
        assert result == payload


async def test_create_notification_includes_link_when_given(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"createNotification": {"id": "n9"}}})
    ) as (client, route):
        await notifications.do_create_notification(
            client,
            "Backup done",
            "Backup",
            "Nightly backup finished OK.",
            "INFO",
            link="https://example.com",
            confirm=True,
        )
        body = json.loads(route.calls.last.request.content)
        assert body["variables"]["input"] == {
            "title": "Backup done",
            "subject": "Backup",
            "description": "Nightly backup finished OK.",
            "importance": "INFO",
            "link": "https://example.com",
        }


# ── Dangerous-tier: array disk ops ───────────────────────────────────────────


async def test_mount_array_disk_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_mount_array_disk(client, "1:sdb", confirm=False)
        assert route.call_count == 0


async def test_mount_array_disk_with_confirm_sends(mocked_client):
    async with mocked_client(
        httpx.Response(
            200,
            json={"data": {"array": {"mountArrayDisk": {"id": "1:sdb", "name": "disk1"}}}},
        )
    ) as (client, route):
        result = await array.do_mount_array_disk(client, "1:sdb", confirm=True)
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.MOUNT_ARRAY_DISK
        assert body["variables"] == {"id": "1:sdb"}
        assert result == {"id": "1:sdb", "name": "disk1"}


async def test_mount_array_disk_rejects_blank_id_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_mount_array_disk(client, "  ", confirm=True)
        assert route.call_count == 0


async def test_unmount_array_disk_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_unmount_array_disk(client, "1:sdb", confirm=False)
        assert route.call_count == 0


async def test_unmount_array_disk_with_confirm_sends(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"array": {"unmountArrayDisk": {"id": "1:sdb"}}}})
    ) as (client, route):
        await array.do_unmount_array_disk(client, "1:sdb", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.UNMOUNT_ARRAY_DISK
        assert body["variables"] == {"id": "1:sdb"}


async def test_clear_disk_statistics_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_clear_disk_statistics(client, "1:sdb", confirm=False)
        assert route.call_count == 0


async def test_clear_disk_statistics_with_confirm_returns_ok(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"array": {"clearArrayDiskStatistics": True}}})
    ) as (client, route):
        result = await array.do_clear_disk_statistics(client, "1:sdb", confirm=True)
        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content)["variables"] == {"id": "1:sdb"}
        assert result == {"ok": True}


async def test_add_disk_to_array_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_add_disk_to_array(client, "1:sdb", confirm=False)
        assert route.call_count == 0


async def test_add_disk_to_array_with_slot_sends_input(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"array": {"addDiskToArray": {"id": "1:x"}}}})
    ) as (client, route):
        await array.do_add_disk_to_array(client, "1:sdb", slot=3, confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.ADD_DISK_TO_ARRAY
        assert body["variables"] == {"input": {"id": "1:sdb", "slot": 3}}


async def test_add_disk_to_array_omits_slot_when_none(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"array": {"addDiskToArray": {"id": "1:x"}}}})
    ) as (client, route):
        await array.do_add_disk_to_array(client, "1:sdb", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["variables"] == {"input": {"id": "1:sdb"}}


async def test_add_disk_to_array_rejects_negative_slot_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_add_disk_to_array(client, "1:sdb", slot=-1, confirm=True)
        assert route.call_count == 0


async def test_array_disk_op_propagates_graphql_error(mocked_client):
    # do_* propagates the domain error; _base.guarded (the @mcp.tool boundary)
    # is what maps it to a friendly, secret-free ToolError for the client.
    async with mocked_client(
        httpx.Response(200, json={"errors": [{"message": "array is started"}], "data": None})
    ) as (client, route):
        with pytest.raises(UnraidGraphQLError):
            await array.do_add_disk_to_array(client, "1:sdb", confirm=True)


# ── Dangerous-tier: docker container removal ─────────────────────────────────


async def test_remove_container_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_remove_container(client, "1:abc", confirm=False)
        assert route.call_count == 0


async def test_remove_container_with_confirm_sends_defaults(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"docker": {"removeContainer": True}}})
    ) as (client, route):
        result = await docker.do_remove_container(client, "1:abc", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.REMOVE_DOCKER_CONTAINER
        assert body["variables"] == {"id": "1:abc", "withImage": False}
        assert result == {"ok": True}


async def test_remove_container_with_image_sends_flag(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"docker": {"removeContainer": True}}})
    ) as (client, route):
        await docker.do_remove_container(client, "1:abc", with_image=True, confirm=True)
        assert json.loads(route.calls.last.request.content)["variables"] == {
            "id": "1:abc",
            "withImage": True,
        }


async def test_remove_container_rejects_blank_id_pre_network(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_remove_container(client, "   ", confirm=True)
        assert route.call_count == 0


async def test_remove_container_propagates_graphql_error(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"errors": [{"message": "no such container"}], "data": None})
    ) as (client, route):
        with pytest.raises(UnraidGraphQLError):
            await docker.do_remove_container(client, "1:abc", confirm=True)


# Each operation must validate its own field, even when another field is populated.
MUTATION_CASES = [
    (array.do_start_array, {}, ("array", "setState")),
    (array.do_stop_array, {}, ("array", "setState")),
    (array.do_start_parity, {"correct": False}, ("parityCheck", "start")),
    (array.do_pause_parity, {}, ("parityCheck", "pause")),
    (array.do_resume_parity, {}, ("parityCheck", "resume")),
    (array.do_cancel_parity, {}, ("parityCheck", "cancel")),
    (array.do_mount_array_disk, {"disk_id": "1:sdb"}, ("array", "mountArrayDisk")),
    (array.do_unmount_array_disk, {"disk_id": "1:sdb"}, ("array", "unmountArrayDisk")),
    (array.do_clear_disk_statistics, {"disk_id": "1:sdb"}, ("array", "clearArrayDiskStatistics")),
    (array.do_add_disk_to_array, {"disk_id": "1:sdb"}, ("array", "addDiskToArray")),
    (docker.do_start_container, {"container_id": "1:a"}, ("docker", "start")),
    (docker.do_stop_container, {"container_id": "1:a"}, ("docker", "stop")),
    (docker.do_restart_container, {"container_id": "1:a"}, ("docker", "restart")),
    (docker.do_pause_container, {"container_id": "1:a"}, ("docker", "pause")),
    (docker.do_unpause_container, {"container_id": "1:a"}, ("docker", "unpause")),
    (docker.do_update_container, {"container_id": "1:a"}, ("docker", "updateContainer")),
    (docker.do_update_containers, {"container_ids": ["1:a"]}, ("docker", "updateContainers")),
    (docker.do_update_all_containers, {}, ("docker", "updateAllContainers")),
    (docker.do_remove_container, {"container_id": "1:a"}, ("docker", "removeContainer")),
    (vm.do_start_vm, {"vm_id": "1:a"}, ("vm", "start")),
    (vm.do_stop_vm, {"vm_id": "1:a"}, ("vm", "stop")),
    (vm.do_pause_vm, {"vm_id": "1:a"}, ("vm", "pause")),
    (vm.do_resume_vm, {"vm_id": "1:a"}, ("vm", "resume")),
    (vm.do_reboot_vm, {"vm_id": "1:a"}, ("vm", "reboot")),
    (vm.do_force_stop_vm, {"vm_id": "1:a"}, ("vm", "forceStop")),
    (vm.do_reset_vm, {"vm_id": "1:a"}, ("vm", "reset")),
    (notifications.do_archive_notification, {"notification_id": "n1"}, ("archiveNotification",)),
    (notifications.do_archive_all, {"importance": None}, ("archiveAll",)),
    (notifications.do_unread_notification, {"notification_id": "n1"}, ("unreadNotification",)),
    (
        notifications.do_delete_notification,
        {"notification_id": "n1", "notification_type": "UNREAD"},
        ("deleteNotification",),
    ),
    (notifications.do_archive_notifications, {"ids": ["n1"]}, ("archiveNotifications",)),
    (notifications.do_unarchive_notifications, {"ids": ["n1"]}, ("unarchiveNotifications",)),
    (notifications.do_unarchive_all, {"importance": None}, ("unarchiveAll",)),
    (notifications.do_delete_archived_notifications, {}, ("deleteArchivedNotifications",)),
    (
        notifications.do_create_notification,
        {"title": "Test", "subject": "Test", "description": "Test", "importance": "INFO"},
        ("createNotification",),
    ),
]


@pytest.mark.parametrize(
    "fn,kwargs,path", MUTATION_CASES, ids=[c[0].__name__ for c in MUTATION_CASES]
)
@pytest.mark.parametrize(
    "response_kind",
    [
        "missing_data",
        "empty_data",
        "null_data",
        "missing_root",
        "null_root",
        "missing_result",
        "null_result",
    ],
)
async def test_mutations_reject_missing_results_at_tool_boundary(
    mocked_client, fn, kwargs, path, response_kind
):
    payload = {}
    if response_kind == "empty_data":
        payload = {"data": {}}
    elif response_kind == "null_data":
        payload = {"data": None}
    elif response_kind == "missing_root":
        payload = {"data": {"other": True}}
    elif response_kind == "null_root":
        payload = {"data": {path[0]: None, "other": True}}
    elif response_kind in ("missing_result", "null_result"):
        result = {} if response_kind == "missing_result" else {path[-1]: None}
        payload = {"data": {path[0]: result}} if len(path) == 2 else {"data": result}
    async with mocked_client(httpx.Response(200, json=payload)) as (client, route):
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client))
        )
        with pytest.raises(ToolError, match="Mutation"):
            await guarded(ctx, fn, confirm=True, **kwargs)
        assert route.call_count == 1


async def test_stop_array_null_result_with_errors_maps_to_tool_error(mocked_client):
    response = {"data": {"array": None}, "errors": [{"message": "cannot stop array"}]}
    async with mocked_client(httpx.Response(200, json=response)) as (client, route):
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client))
        )
        with pytest.raises(ToolError, match="cannot stop array"):
            await guarded(ctx, array.do_stop_array, confirm=True)
        assert route.call_count == 1


async def test_restart_fallback_rejects_empty_stop_before_start(mocked_client):
    unsupported = httpx.Response(
        200,
        json={"errors": [{"message": 'Cannot query field "restart" on type "DockerMutations".'}]},
    )
    empty_stop = httpx.Response(200, json={"data": {"docker": {"stop": None}}})
    async with mocked_client([unsupported, empty_stop]) as (client, route):
        with pytest.raises(UnraidServerError, match="docker.stop"):
            await docker.do_restart_container(client, "1:a", confirm=True)
        assert route.call_count == 2


@pytest.mark.parametrize(
    "fn,kwargs,path", MUTATION_CASES, ids=[c[0].__name__ for c in MUTATION_CASES]
)
async def test_all_mutations_refuse_without_confirm_before_network(mocked_client, fn, kwargs, path):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError, match="confirm"):
            await fn(client, confirm=False, **kwargs)
        assert route.call_count == 0


async def test_parity_tools_accept_list_result(mocked_client):
    for fn, field in [
        (array.do_pause_parity, "pause"),
        (array.do_resume_parity, "resume"),
        (array.do_cancel_parity, "cancel"),
    ]:
        resp = httpx.Response(200, json={"data": {"parityCheck": {field: [{"date": "d"}]}}})
        async with mocked_client(resp) as (client, _route):
            assert await fn(client, confirm=True) == {"ok": True, "history_count": 1}
