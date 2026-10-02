"""Filters + concise/full detail on the list tools (#158)."""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp.formatting import (
    shape_containers,
    shape_physical_disks,
    shape_shares,
    shorten_container_ids,
)
from unraid_mcp.server import build_server
from unraid_mcp.tools import array, docker, shares, vm
from unraid_mcp.tools._base import compact_text

URL = "https://tower.local/graphql"


def _container(name, state, update, cid):
    return {
        "id": f"1:{cid}",
        "names": [f"/{name}"],
        "image": f"{name}:latest",
        "state": state,
        "status": "Up" if state == "RUNNING" else "Exited",
        "autoStart": True,
        "isUpdateAvailable": update,
        "isOrphaned": False,
        "webUiUrl": None,
        "hostConfig": {"networkMode": "bridge"},
        "ports": [{"privatePort": 80, "publicPort": 8080, "type": "TCP", "ip": "0.0.0.0"}],
    }


CONTAINERS = {
    "docker": {
        "containers": [
            _container("Plex", "RUNNING", True, "a"),
            _container("plex-meta", "EXITED", False, "b"),
            _container("sonarr", "RUNNING", False, "c"),
            _container("radarr", "PAUSED", None, "d"),
        ]
    }
}


def _disk(did, name, device, dtype, smart):
    return {
        "id": f"1:{did}",
        "name": name,
        "device": device,
        "vendor": "WDC",
        "type": dtype,
        "size": 1024**4,
        "interfaceType": "SATA",
        "smartStatus": smart,
        "temperature": 30,
        "isSpinning": True,
        "serialNum": f"S{did}",
    }


DISKS = {
    "disks": [
        _disk("d1", "WDC WD80", "/dev/sda", "HD", "OK"),
        _disk("d2", "Samsung 980", "/dev/nvme0n1", "SSD", "OK"),
        _disk("d3", "WDC WD40", "/dev/sdb", "HD", "UNKNOWN"),
    ]
}

VMS = {
    "vms": {
        "id": "vms",
        "domains": [
            {"id": "v1", "name": "Windows 11", "state": "RUNNING"},
            {"id": "v2", "name": "windows-old", "state": "SHUTOFF"},
            {"id": "v3", "name": "HomeAssistant", "state": "RUNNING"},
        ],
    }
}

SHARES = {
    "shares": [
        {
            "id": f"1:{n}",
            "name": n,
            "free": 1024,
            "used": 2048,
            "size": 4096,
            "comment": "c",
            "allocator": "highwater",
            "cache": True,
            "include": ["disk1"],
        }
        for n in ("appdata", "Media", "media-backup", "isos")
    ]
}

DATA = {**CONTAINERS, **DISKS, **VMS, **SHARES}

# tool -> (fetch fn, current full shape, concise key set)
TOOLS = {
    "list_docker_containers": (
        docker.fetch_containers,
        shorten_container_ids(shape_containers(CONTAINERS)),
        {"id", "name", "image", "state", "status", "update_available", "web_ui_url"},
    ),
    "list_disks": (
        array.fetch_disks,
        shape_physical_disks(DISKS),
        {"id", "name", "device", "type", "smart_status", "temp_c", "spinning", "size"},
    ),
    "list_shares": (shares.fetch_shares, shape_shares(SHARES), {"name", "free", "used", "size"}),
}


def _ok():
    return httpx.Response(200, json={"data": DATA})


def _names(items):
    return [i["name"] for i in items]


# ── logic layer: filters ─────────────────────────────────────────────────────

FILTER_CASES = [
    # (fetch fn, kwargs, expected names)
    (docker.fetch_containers, {"name": "PLEX"}, ["Plex", "plex-meta"]),
    (docker.fetch_containers, {"state": "RUNNING"}, ["Plex", "sonarr"]),
    (docker.fetch_containers, {"update_available": True}, ["Plex"]),
    (docker.fetch_containers, {"update_available": False}, ["plex-meta", "sonarr"]),
    (docker.fetch_containers, {"name": "plex", "state": "RUNNING"}, ["Plex"]),
    (docker.fetch_containers, {"name": "plex", "update_available": False}, ["plex-meta"]),
    (docker.fetch_containers, {"name": "nope"}, []),
    (docker.fetch_containers, {"state": "PAUSED", "name": "sonarr"}, []),
    (array.fetch_disks, {"name": "wdc"}, ["WDC WD80", "WDC WD40"]),
    (array.fetch_disks, {"name": "nvme"}, ["Samsung 980"]),
    (array.fetch_disks, {"disk_type": "hd"}, ["WDC WD80", "WDC WD40"]),
    (array.fetch_disks, {"smart_status": "UNKNOWN"}, ["WDC WD40"]),
    (array.fetch_disks, {"disk_type": "HD", "smart_status": "OK"}, ["WDC WD80"]),
    (array.fetch_disks, {"name": "samsung", "disk_type": "HD"}, []),
    (vm.fetch_vms, {"name": "windows"}, ["Windows 11", "windows-old"]),
    (vm.fetch_vms, {"state": "RUNNING"}, ["Windows 11", "HomeAssistant"]),
    (vm.fetch_vms, {"name": "windows", "state": "SHUTOFF"}, ["windows-old"]),
    (vm.fetch_vms, {"state": "PAUSED"}, []),
    # Enum filters are case-insensitive.
    (docker.fetch_containers, {"state": "running"}, ["Plex", "sonarr"]),
    (array.fetch_disks, {"smart_status": "unknown"}, ["WDC WD40"]),
    (vm.fetch_vms, {"state": "shutoff"}, ["windows-old"]),
    (shares.fetch_shares, {"name": "MEDIA"}, ["Media", "media-backup"]),
    (shares.fetch_shares, {"name": "zzz"}, []),
]


@pytest.mark.parametrize("fn,kwargs,expected", FILTER_CASES)
async def test_filters(mocked_client, fn, kwargs, expected):
    async with mocked_client(_ok()) as (client, route):
        out = await fn(client, **kwargs)
    assert _names(out) == expected
    assert route.call_count == 1


@pytest.mark.parametrize("tool", TOOLS)
async def test_full_equals_current_output(mocked_client, tool):
    fn, full, _ = TOOLS[tool]
    async with mocked_client(_ok()) as (client, _route):
        assert await fn(client, detail="full") == full
        # Logic default stays full (internal callers rely on it).
        assert await fn(client) == full


@pytest.mark.parametrize("tool", TOOLS)
async def test_concise_key_set(mocked_client, tool):
    fn, full, keys = TOOLS[tool]
    async with mocked_client(_ok()) as (client, _route):
        out = await fn(client, detail="concise")
    assert out and all(set(item) == keys for item in out)
    assert out == [{k: v for k, v in item.items() if k in keys} for item in full]


async def test_filters_drop_null_items_but_unfiltered_keeps_them(mocked_client):
    data = {"docker": {"containers": [None, _container("a", "RUNNING", None, "x")]}}
    async with mocked_client(httpx.Response(200, json={"data": data})) as (client, _):
        assert (await docker.fetch_containers(client, detail="concise"))[0] is None
        assert _names(await docker.fetch_containers(client, state="RUNNING")) == ["a"]


async def test_name_lookup_still_uses_full_list(mocked_client):
    async with mocked_client(_ok()) as (client, _):
        # Native lookup returns nothing in this fixture, so the list row is used.
        out = await docker._resolve_container(client, "sonarr")
    assert "ports" in out and "names" in out


@pytest.mark.parametrize(
    "fn,kwargs,param",
    [
        (docker.fetch_containers, {"state": "runnin"}, "state"),
        (docker.fetch_containers, {"detail": "brief"}, "detail"),
        (array.fetch_disks, {"smart_status": "FAILED"}, "smart_status"),
        (array.fetch_disks, {"detail": "all"}, "detail"),
        (vm.fetch_vms, {"state": "stopped"}, "state"),
        (shares.fetch_shares, {"detail": "x"}, "detail"),
    ],
)
async def test_logic_rejects_invalid_enum_pre_network(mocked_client, fn, kwargs, param):
    async with mocked_client(_ok()) as (client, route):
        with pytest.raises(ToolError, match=f"Invalid {param} .*Must be one of: "):
            await fn(client, **kwargs)
    assert route.call_count == 0


# ── MCP layer ────────────────────────────────────────────────────────────────


async def _session_call(settings_factory, tool, args, data=DATA):
    with respx.mock:
        route = respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(settings_factory()), raise_exceptions=False) as s:
            route.calls.clear()  # ignore the lifespan version probe
            result = await s.call_tool(tool, args)
            return result, route


@pytest.mark.parametrize(
    "tool,args,allowed",
    [
        ("list_docker_containers", {"state": "garbage"}, "'RUNNING', 'PAUSED' or 'EXITED'"),
        ("list_docker_containers", {"state": 1}, "'RUNNING', 'PAUSED' or 'EXITED'"),
        ("list_docker_containers", {"detail": "brief"}, "'concise' or 'full'"),
        ("list_disks", {"smart_status": "bad"}, "'OK' or 'UNKNOWN'"),
        ("list_vms", {"state": "stopped"}, "'SHUTOFF'"),
        ("list_shares", {"detail": "all"}, "'concise' or 'full'"),
    ],
)
async def test_sdk_rejects_invalid_enum_without_http(settings_factory, tool, args, allowed):
    result, route = await _session_call(settings_factory, tool, args)
    assert result.is_error is True
    assert allowed in result.content[0].text
    assert route.call_count == 0


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("list_docker_containers", {"state": "running"}, ["Plex", "sonarr"]),
        ("list_docker_containers", {"state": "Exited"}, ["plex-meta"]),
        ("list_disks", {"smart_status": "unknown"}, ["WDC WD40"]),
        ("list_vms", {"state": "shutoff"}, ["windows-old"]),
    ],
)
async def test_sdk_enum_filters_case_insensitive(settings_factory, tool, args, expected):
    result, route = await _session_call(settings_factory, tool, args)
    assert result.is_error is False, result.content[0].text
    assert _names(result.structured_content["result"]) == expected
    assert route.call_count == 1


async def test_enum_schema_stays_uppercase(settings_factory):
    with respx.mock:
        respx.post(URL).respond(200, json={"data": {}})
        async with Client(build_server(settings_factory())) as s:
            tools = {t.name: t for t in (await s.list_tools()).tools}
    props = tools["list_docker_containers"].input_schema["properties"]
    assert ["RUNNING", "PAUSED", "EXITED"] in [
        branch.get("enum") for branch in props["state"].get("anyOf", [props["state"]])
    ]


async def test_only_container_disk_share_lists_take_detail(settings_factory):
    flags = {"allow_mutations": True, "allow_dangerous": True, "allow_raw_query": True}
    with respx.mock:
        respx.post(URL).respond(200, json={"data": {}})
        async with Client(build_server(settings_factory(**flags))) as s:
            tools = (await s.list_tools()).tools
    with_detail = {t.name for t in tools if "detail" in t.input_schema.get("properties", {})}
    assert with_detail == {"list_docker_containers", "list_disks", "list_shares"}


async def test_get_tools_keep_strict_output_contracts(settings_factory):
    """List items relax non-concise keys; get_* contracts stay fully required."""
    with respx.mock:
        respx.post(URL).respond(200, json={"data": {}})
        async with Client(build_server(settings_factory())) as s:
            schemas = {t.name: t.output_schema for t in (await s.list_tools()).tools}
    assert set(schemas["get_disk"]["required"]) == {
        "id", "name", "device", "vendor", "type", "serial", "interface",
        "smart_status", "temp_c", "spinning", "size", "firmware", "partitions",
    }  # fmt: skip
    assert set(schemas["get_docker_container"]["required"]) == {
        "id", "name", "names", "image", "state", "status", "auto_start",
        "auto_start_order", "update_available", "orphaned", "web_ui_url",
        "network_mode", "ports",
    }  # fmt: skip
    for tool, keys in (("list_docker_containers", 0), ("list_disks", 1)):
        item = schemas[tool]["properties"]["result"]["items"]["anyOf"][0]
        definition = schemas[tool]["$defs"][item["$ref"].split("/")[-1]]
        concise = list(TOOLS.values())[keys][2]
        assert set(definition["required"]) == concise


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("detail", [None, "concise", "full"])
async def test_both_detail_modes_validate_through_sdk(settings_factory, tool, detail):
    _, full, keys = TOOLS[tool]
    args = {} if detail is None else {"detail": detail}
    result, route = await _session_call(settings_factory, tool, args)
    assert result.is_error is False, result.content[0].text
    assert route.call_count == 1
    items = result.structured_content["result"]
    if detail == "full":
        assert items == full
    else:  # default is concise
        assert all(set(item) == keys for item in items)
    assert result.content[0].text == compact_text(items)


async def test_concise_is_much_smaller_than_full(settings_factory):
    full, _ = await _session_call(settings_factory, "list_docker_containers", {"detail": "full"})
    concise, _ = await _session_call(settings_factory, "list_docker_containers", {})
    assert len(concise.content[0].text) < 0.6 * len(full.content[0].text)


async def test_filters_apply_through_sdk(settings_factory):
    result, _ = await _session_call(
        settings_factory, "list_docker_containers", {"name": "plex", "state": "RUNNING"}
    )
    assert result.structured_content["result"] == [
        {
            "id": "a",
            "name": "Plex",
            "image": "Plex:latest",
            "state": "RUNNING",
            "status": "Up",
            "update_available": True,
            "web_ui_url": None,
        }
    ]
    result, _ = await _session_call(settings_factory, "list_disks", {"type": "ssd"})
    assert _names(result.structured_content["result"]) == ["Samsung 980"]
    result, _ = await _session_call(settings_factory, "list_vms", {"name": "zzz"})
    assert result.structured_content["result"] == []
