"""Bare ids everywhere (#174): no `<serverId>:` prefix in any output, and every
id-taking tool accepts the bare and the prefixed form."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitResult

from tests.conftest import URL
from unraid_mcp.formatting import strip_server_prefix, strip_server_prefixes
from unraid_mcp.server import build_server
from unraid_mcp.tools import array, misc, notifications, vm
from unraid_mcp.tools._base import local_id

SERVER = "3f9a" * 16  # sha256 hex, like upstream's getServerIdentifier()
PREFIXED = re.compile(r"[0-9a-fA-F]{64}:")
CID = "4607a3dbd171" + "5" * 52
SERIAL = "WD-WCC7K1234567"
VM_UUID = "6c3b2a1e-0d4f-4a8b-9c7e-123456789abc"
NOTE = "Disk_warning_1700000000.notify"


def p(local: str) -> str:
    return f"{SERVER}:{local}"


def _resp(data):
    return httpx.Response(200, json={"data": data})


def _body(call):
    return json.loads(call.request.content)


# ── Helper ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (p(SERIAL), SERIAL),
        (p(VM_UUID), VM_UUID),
        (p(NOTE), NOTE),
        (f"{SERVER.upper()}:{SERIAL}", SERIAL),
        (SERIAL, SERIAL),
        (CID, CID),
        ("1:disk", "1:disk"),  # not a server prefix
        ("coretemp-isa-0000:Core 0:temp2_input", "coretemp-isa-0000:Core 0:temp2_input"),
        (p("a:b"), p("a:b")),  # upstream never prefixes a local id containing ':'
        (f"{SERVER}:", f"{SERVER}:"),
        (None, None),
        (7, 7),
    ],
)
def test_strip_server_prefix(value, expected):
    assert strip_server_prefix(value) == expected


def test_strip_server_prefixes_only_touches_id_fields():
    data = {
        "vms": {"id": p("vms"), "domains": [{"id": p(VM_UUID), "name": p("x")}, None]},
        "docker": {"logs": {"containerId": p(CID), "lines": [{"message": p("m")}]}},
        "n": 1,
    }
    out = strip_server_prefixes(data)
    assert out == {
        "vms": {"id": "vms", "domains": [{"id": VM_UUID, "name": p("x")}, None]},
        "docker": {"logs": {"containerId": CID, "lines": [{"message": p("m")}]}},
        "n": 1,
    }
    assert data["vms"]["id"] == p("vms")  # input untouched


@pytest.mark.parametrize("value", [p(SERIAL), SERIAL, f"  {p(SERIAL)}\n", f"\t{SERIAL} "])
def test_local_id_accepts_both_forms_and_keeps_case(value):
    assert local_id(value) == SERIAL


async def test_client_strips_prefixes(mocked_client):
    async with mocked_client(_resp({"disks": [{"id": p(SERIAL)}]})) as (client, _):
        assert await client.execute("query Q { disks { id } }") == {"disks": [{"id": SERIAL}]}
        data, errors = await client.execute_with_errors("query Q { disks { id } }")
    assert data == {"disks": [{"id": SERIAL}]} and errors == []


# ── Every read tool and resource: no server prefix in output ────────────────

CONTAINER = {
    "id": p(CID),
    "names": ["/plex"],
    "image": "plex:latest",
    "state": "RUNNING",
    "status": "Up",
    "autoStart": True,
    "ports": [],
}
NOTIFICATION = {
    "id": p(NOTE),
    "title": "Disk warning",
    "subject": "s",
    "description": "d",
    "importance": "WARNING",
    "type": "UNREAD",
    "timestamp": "2026-01-01T00:00:00Z",
}
COUNTS = {"info": 0, "warning": 1, "alert": 0, "total": 1}
OPS = {
    "GetApiProbe": {"info": {"versions": {"core": {"api": "4.37.4", "unraid": "7.3.2"}}}},
    "GetSystemInfo": {"info": {"os": {"hostname": "tower"}, "time": "2026-01-01T00:00:00Z"}},
    "GetFlashInfo": {"flash": {"guid": "0781-5571", "vendor": "v", "product": "p"}},
    "GetSystemMetrics": {
        "metrics": {"cpu": {"percentTotal": 1.0, "cpus": []}, "temperature": {"sensors": []}}
    },
    "GetSystemMetricsNetwork": {"metrics": {"network": [{"name": "eth0"}]}},
    "GetHealthTemperature": {
        "metrics": {
            "temperature": {
                "sensors": [
                    {
                        "id": "coretemp-isa-0000:Package id 0:temp1_input",
                        "name": "CPU",
                        "type": "CPU_PACKAGE",
                        "current": {"value": 40, "unit": "CELSIUS", "status": "NORMAL"},
                    }
                ]
            }
        }
    },
    "GetServices": {"services": [{"id": p("unraid-api"), "name": "unraid-api", "online": True}]},
    "GetArrayStatus": {
        "array": {
            "id": p("array"),
            "state": "STARTED",
            "capacity": {"kilobytes": {"total": "4", "used": "1", "free": "3"}},
            "disks": [{"id": p("WDC_WD40_" + SERIAL), "name": "disk1", "status": "DISK_OK"}],
            "parities": [],
            "caches": [],
            "bootDevices": [],
            "parityCheckStatus": {"status": "NEVER_RUN"},
        }
    },
    "GetParityStatus": {"array": {"parityCheckStatus": {"status": "NEVER_RUN"}}},
    "GetParityHistory": {"parityHistory": []},
    "ListPhysicalDisks": {"disks": [{"id": p(SERIAL), "serialNum": SERIAL, "name": "WD"}]},
    "GetDiskDetails": {"disk": {"id": p(SERIAL), "serialNum": SERIAL, "partitions": []}},
    "ListDockerContainers": {"docker": {"containers": [CONTAINER]}},
    "DockerContainerRefs": {"docker": {"containers": [{"id": p(CID), "names": ["/plex"]}]}},
    "GetDockerContainer": {"docker": {"container": CONTAINER}},
    "GetDockerPortConflicts": {
        "docker": {
            "portConflicts": {
                "containerPorts": [
                    {"privatePort": 80, "type": "TCP", "containers": [{"id": p(CID), "name": "a"}]}
                ],
                "lanPorts": [],
            }
        }
    },
    "GetDockerNetworks": {"docker": {"networks": [{"id": p("b" * 64), "name": "bridge"}]}},
    "GetContainerLogs": {"docker": {"logs": {"containerId": p(CID), "lines": [], "cursor": None}}},
    "GetDockerUpdateStatuses": {
        "docker": {
            "containerUpdateStatuses": [{"name": "plex", "updateStatus": "UP_TO_DATE"}],
            "containers": [{"id": p(CID), "names": ["/plex"]}],
        }
    },
    "ListVMs": {
        "vms": {"id": p("vms"), "domains": [{"id": p(VM_UUID), "name": "w", "state": "X"}]}
    },
    "GetSharesInfo": {"shares": [{"id": p("appdata"), "name": "appdata"}]},
    "GetSystemTime": {"systemTime": {"currentTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"}},
    "GetHardwareInventory": {
        "info": {
            "devices": {
                "gpu": [{"id": p("gpu-0"), "type": "VGA"}],
                "pci": [{"id": p("pci-0000"), "type": "Bridge"}],
                "usb": [{"id": p("usb-1-1"), "name": "hub"}],
                "network": [{"id": p("net-eth0"), "iface": "eth0"}],
            }
        }
    },
    "GetNotificationsOverview": {
        "notifications": {"overview": {"unread": COUNTS, "archive": COUNTS}}
    },
    "ListNotifications": {"notifications": {"list": [NOTIFICATION]}},
    "GetWarningsAndAlerts": {"notifications": {"warningsAndAlerts": [NOTIFICATION]}},
    "GetUpsDevices": {"upsDevices": [{"id": "ups0", "name": "ups", "status": "ONLINE"}]},
    "GetConnectStatus": {"registration": {"id": p("registration"), "type": "PRO"}},
    "GetNetworkInterfaces": {"networkInterfaces": [{"id": p("eth0"), "name": "eth0"}]},
    "GetMe": {"me": {"id": p("root"), "name": "root", "roles": ["ADMIN"]}},
    "ListPlugins": {"plugins": [{"name": "unraid-api-plugin-connect", "version": "1"}]},
    "GetInstalledUnraidPlugins": {"installedUnraidPlugins": []},
    "GetLogFiles": {"logFiles": [{"name": "syslog", "path": "/var/log/syslog"}]},
    "GetLogFile": {
        "logFile": {"path": "/var/log/syslog", "content": "x\n", "totalLines": 1, "startLine": 1}
    },
}
OPS["GetUpsDevicesLegacy"] = OPS["GetUpsDevices"]
OPS["GetArrayStatusLegacy"] = OPS["GetArrayStatus"]
_OP = re.compile(r"\b(?:query|mutation)\s+(\w+)")


def _dispatch(request):
    name = _OP.search(json.loads(request.content)["query"]).group(1)
    if name not in OPS:
        return httpx.Response(200, json={"data": None, "errors": [{"message": f"no op {name}"}]})
    return _resp(OPS[name])


READ_CALLS = {
    "check_docker_updates": {},
    "get_array_status": {},
    "get_connect_status": {},
    "get_disk": {"disk_id": p(SERIAL)},
    "get_docker_container": {"identifier": CID[:12]},
    "get_docker_container_logs": {"container_id": CID},
    "get_docker_port_conflicts": {},
    "get_hardware_inventory": {},
    "get_health_summary": {},
    "get_notifications_overview": {},
    "get_parity_history": {},
    "get_parity_status": {},
    "get_services": {},
    "get_system_info": {},
    "get_system_metrics": {},
    "get_system_time": {},
    "get_ups_status": {},
    "list_disks": {"detail": "full"},
    "list_docker_containers": {"detail": "full"},
    "list_docker_networks": {},
    "list_log_files": {},
    "list_network_interfaces": {},
    "list_notifications": {},
    "list_plugins": {},
    "list_shares": {"detail": "full"},
    "list_vms": {},
    "list_warnings_and_alerts": {},
    "read_log_file": {"path": "/var/log/syslog"},
    "whoami": {},
}
# Stats come from a websocket subscription, shortened in test_tools_stats.py.
# run_graphql_query returns raw upstream data on purpose (see the raw tests below).
NOT_HTTP = {"get_docker_container_stats", "run_graphql_query"}


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content) + json.dumps(
        result.structured_content
    )


async def test_no_read_tool_or_resource_output_carries_a_server_prefix(settings_factory):
    settings = settings_factory(allow_mutations=True, allow_raw_query=True)
    with respx.mock:
        respx.post(URL).mock(side_effect=_dispatch)
        mcp = build_server(settings)
        async with Client(mcp) as session:
            tools = (await session.list_tools()).tools
            read_tools = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
            assert read_tools - NOT_HTTP == set(READ_CALLS)
            for name, args in READ_CALLS.items():
                result = await session.call_tool(name, args)
                assert not result.is_error, (name, result.content)
                text = _text(result)
                assert not PREFIXED.search(text), (name, text)
            for uri in ("unraid://health", "unraid://system-info"):
                res = await session.read_resource(uri)
                text = "".join(getattr(c, "text", "") for c in res.contents)
                assert not PREFIXED.search(text), (uri, text)


async def test_bare_ids_are_what_list_tools_emit(mocked_client):
    responses = [_resp(OPS[op]) for op in ("ListPhysicalDisks", "ListVMs", "ListNotifications")]
    async with mocked_client(responses) as (client, _):
        disks = await array.fetch_disks(client, detail="full")
        vms = await vm.fetch_vms(client)
        notes = await notifications.fetch_notifications(client)
    assert disks[0]["id"] == SERIAL
    assert vms[0]["id"] == VM_UUID
    assert notes[0]["id"] == NOTE


# ── Inputs: both forms work; mutations refuse before any request ───────────


@pytest.mark.parametrize("ident", [p(SERIAL), SERIAL, f" {SERIAL} "])
async def test_get_disk_accepts_both_forms(mocked_client, ident):
    async with mocked_client(_resp(OPS["GetDiskDetails"])) as (client, route):
        out = await array.fetch_disk(client, ident)
    assert _body(route.calls[0])["variables"] == {"id": SERIAL}
    assert out["id"] == SERIAL


async def test_get_disk_not_found_names_caller_id(mocked_client):
    err = httpx.Response(
        200, json={"data": None, "errors": [{"message": f"Disk with id {SERIAL} not found"}]}
    )
    async with mocked_client(err) as (client, _):
        with pytest.raises(ToolError, match=re.escape(f"'{p(SERIAL)}'")):
            await array.fetch_disk(client, p(SERIAL))


VM_CALLS = {
    "vm_power": lambda c, i, ok: vm.do_vm_power(c, i, "start", ok),
    "stop": lambda c, i, ok: vm.do_stop_vm(c, i, ok),
    "reboot": lambda c, i, ok: vm.do_reboot_vm(c, i, ok),
    "force_stop": lambda c, i, ok: vm.do_force_stop_vm(c, i, ok),
    "reset": lambda c, i, ok: vm.do_reset_vm(c, i, ok),
    "pause": lambda c, i, ok: vm.do_pause_vm(c, i, ok),
    "resume": lambda c, i, ok: vm.do_resume_vm(c, i, ok),
}
VM_RESULT = {
    "vm": dict.fromkeys(("start", "stop", "reboot", "forceStop", "reset", "pause", "resume"), True)
}
NOTE_CALLS = {
    "archive": lambda c, i, ok: notifications.do_notification_archive(c, i, "archive", ok),
    "unarchive": lambda c, i, ok: notifications.do_notification_archive(c, i, "unarchive", ok),
    "delete": lambda c, i, ok: notifications.do_delete_notification(c, i, "ARCHIVE", ok),
}
NOTE_RESULT = {
    "archiveNotification": NOTIFICATION,
    "unreadNotification": NOTIFICATION,
    "deleteNotification": {"unread": COUNTS, "archive": COUNTS},
}
DISK_CALLS = {
    "mount": lambda c, i, ok: array.do_mount_array_disk(c, i, ok),
    "unmount": lambda c, i, ok: array.do_unmount_array_disk(c, i, ok),
    "clear_stats": lambda c, i, ok: array.do_clear_disk_statistics(c, i, ok),
    "add": lambda c, i, ok: array.do_add_disk_to_array(c, i, confirm=ok),
}
DISK_RESULT = {
    "array": {
        k: {"id": p(SERIAL), "name": "disk1", "status": "DISK_OK"}
        for k in ("mountArrayDisk", "unmountArrayDisk")
    }
    | {"clearArrayDiskStatistics": True, "addDiskToArray": {"id": p("array"), "state": "STOPPED"}}
}
MUTATIONS = [
    *[(f"vm_{k}", fn, VM_UUID, VM_RESULT) for k, fn in VM_CALLS.items()],
    *[(f"notification_{k}", fn, NOTE, NOTE_RESULT) for k, fn in NOTE_CALLS.items()],
    *[(f"disk_{k}", fn, SERIAL, DISK_RESULT) for k, fn in DISK_CALLS.items()],
]


def _sent_id(body):
    variables = body["variables"]
    return variables["input"]["id"] if "input" in variables else variables["id"]


@pytest.mark.parametrize("form", ["prefixed", "bare", "padded"])
@pytest.mark.parametrize("_name,call,local,result", MUTATIONS, ids=[m[0] for m in MUTATIONS])
async def test_mutations_accept_both_forms(mocked_client, _name, call, local, result, form):
    ident = {"prefixed": p(local), "bare": local, "padded": f" {local}\n"}[form]
    async with mocked_client(_resp(result)) as (client, route):
        out = await call(client, ident, True)
    assert route.call_count == 1
    assert _sent_id(_body(route.calls[0])) == local
    assert not PREFIXED.search(json.dumps(out))


@pytest.mark.parametrize("form", ["prefixed", "bare"])
@pytest.mark.parametrize("_name,call,local,_result", MUTATIONS, ids=[m[0] for m in MUTATIONS])
async def test_mutation_without_confirm_makes_no_request(
    mocked_client, _name, call, local, _result, form
):
    ident = p(local) if form == "prefixed" else local
    async with mocked_client(_resp({})) as (client, route):
        with pytest.raises(ToolError, match="confirm=true") as exc:
            await call(client, ident, False)
    assert route.call_count == 0
    assert ident in str(exc.value)  # consequence names the caller-supplied id


@pytest.mark.parametrize("form", ["prefixed", "bare"])
@pytest.mark.parametrize("action", ["archive", "unarchive"])
async def test_bulk_notification_ids_accept_both_forms(mocked_client, action, form):
    ids = [p(NOTE), "Other_1.notify"] if form == "prefixed" else [NOTE, " Other_1.notify "]
    key = "archiveNotifications" if action == "archive" else "unarchiveNotifications"
    done = {key: {"unread": COUNTS, "archive": COUNTS}}
    async with mocked_client(_resp(done)) as (client, route):
        await notifications.do_notification_archive_bulk(client, ids, action, True)
    assert _body(route.calls[0])["variables"] == {"ids": [NOTE, "Other_1.notify"]}
    async with mocked_client(_resp(done)) as (client, route):
        with pytest.raises(ToolError, match="confirm=true"):
            await notifications.do_notification_archive_bulk(client, ids, action, False)
    assert route.call_count == 0


async def test_destructive_vm_elicits_with_caller_id_before_any_request(settings_factory):
    messages = []

    async def elicit(context, params):
        assert len(respx.calls) == 0
        messages.append(params.message)
        return ElicitResult(action="accept", content={"proceed": True})

    with respx.mock:
        route = respx.post(URL).mock(side_effect=[_resp({}), _resp(VM_RESULT)])
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, mode="auto", elicitation_callback=elicit) as client:
            respx.calls.clear()
            result = await client.call_tool("force_stop_vm", {"vm_id": p(VM_UUID), "confirm": True})
    assert not result.is_error, result.content
    assert any(f"force-stop VM '{p(VM_UUID)}'" in m for m in messages)
    assert _body(route.calls[-1])["variables"] == {"id": VM_UUID}


# ── run_graphql_query: raw upstream data, prefixes kept ─────────────────────

RAW = {
    "vms": {"id": p("vms"), "domains": [{"local": p(VM_UUID), "id": p(VM_UUID)}]},
    "id": {"id": p("nested")},  # `id: vms { ... }` alias
    "notifications": {"list": [{"id": p(NOTE), "description": p("free text")}]},
}


async def test_raw_query_returns_ids_unstripped(mocked_client):
    async with mocked_client(_resp(RAW)) as (client, _):
        out = await misc.do_raw_query(client, "query { vms { id } }")
    assert out == RAW


async def test_raw_query_tool_keeps_prefixes_end_to_end(settings_factory):
    with respx.mock:
        respx.post(URL).mock(return_value=_resp(RAW))
        mcp = build_server(settings_factory(allow_raw_query=True))
        async with Client(mcp) as session:
            result = await session.call_tool("run_graphql_query", {"query": "query { vms { id } }"})
    assert not result.is_error, result.content
    assert result.structured_content == RAW


async def test_client_opt_out_keeps_aliases_and_text_untouched(mocked_client):
    async with mocked_client(_resp(RAW)) as (client, _):
        raw = await client.execute("query { x }", strip_prefixes=False)
        data, _ = await client.execute_with_errors("query { x }", strip_prefixes=False)
    assert raw == data == RAW


async def test_bulk_notification_ids_deduped_after_normalizing(mocked_client):
    done = {"archiveNotifications": {"unread": COUNTS, "archive": COUNTS}}
    ids = [p(NOTE), NOTE, f" {NOTE} ", "Other_1.notify"]
    async with mocked_client(_resp(done)) as (client, route):
        await notifications.do_notification_archive_bulk(client, ids, "archive", True)
    assert _body(route.calls[0])["variables"] == {"ids": [NOTE, "Other_1.notify"]}
