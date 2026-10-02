"""Typed core outputs advertised and validated through the in-memory SDK client."""

import json
from copy import deepcopy

import pytest
import respx
from mcp.client import Client

from unraid_mcp.formatting import (
    shape_container,
    shape_container_detail,
    shape_physical_disk,
    summarize_health,
)
from unraid_mcp.server import build_server
from unraid_mcp.tools import array, docker, misc
from unraid_mcp.tools._base import compact_text

URL = "https://tower.local/graphql"
TOOLS = [
    ("get_health_summary", {}),
    ("list_docker_containers", {"detail": "full"}),
    ("get_docker_container", {"identifier": "1:abc"}),
    ("list_disks", {"detail": "full"}),
    ("get_disk", {"disk_id": "1:disk"}),
]
CONTAINER = {
    "id": "1:abc",
    "names": ["/plex", "/media"],
    "image": "plex:latest",
    "state": "RUNNING",
    "status": "Up 1 hour",
    "autoStart": True,
    "ports": [{"privatePort": 32400, "publicPort": 32400, "type": "TCP", "ip": "0.0.0.0"}],
}
DISK = {
    "id": "1:disk",
    "name": "disk model",
    "device": "/dev/sda",
    "vendor": "vendor",
    "type": "HDD",
    "serialNum": "serial",
    "interfaceType": "SATA",
    "smartStatus": "OK",
    "temperature": 31,
    "isSpinning": True,
    "size": 1024**4,
    "firmwareRevision": "1.0",
    "partitions": [{"name": "sda1", "fsType": "XFS", "size": 1024}],
}
HEALTH = {
    "overall": "critical",
    "reasons": ["Disk disk1 is failed", "Unread warning notifications: 1"],
    "checks": {"array": "ok", "ups": "ok", "notifications": "ok", "temperature": "ok"},
    "array_state": "STARTED",
    "capacity": {
        "total": {"bytes": 4096, "human": "4.0 KiB"},
        "used": {"bytes": 1024, "human": "1.0 KiB"},
        "free": {"bytes": 3072, "human": "3.0 KiB"},
    },
    "disk_count": 1,
    "unhealthy_disks": [{"name": "disk1", "health": "failed", "status": "DISK_DSBL"}],
    "parity_check": {
        "progress": 50,
        "speed": "100 MB/s",
        "errors": 0,
        "status": "RUNNING",
        "paused": False,
        "running": True,
        "correcting": False,
    },
    "ups": [{"name": "ups", "status": "ONLINE", "battery_pct": 100}],
    "notifications_unread": {"info": 0, "warning": 1, "alert": 0, "total": 1},
    "top_alerts": [{"title": "Disk warning", "importance": "WARNING"}],
    "temperature": {
        "hottest": {"name": "CPU", "value": 55.0, "unit": "CELSIUS", "level": "normal"},
        "warning_count": 0,
        "critical_count": 0,
        "ignored_count": 0,
    },
}


def _fixture(kind):
    if kind == "full":
        container, disk = deepcopy(CONTAINER), deepcopy(DISK)
        data = {
            "array": {
                "state": "STARTED",
                "capacity": {"kilobytes": {"total": "4", "used": "1", "free": "3"}},
                "disks": [{"name": "disk1", "status": "DISK_DSBL"}],
                "parityCheckStatus": HEALTH["parity_check"],
            },
            "upsDevices": [{"name": "ups", "status": "ONLINE", "battery": {"chargeLevel": 100}}],
            "metrics": {
                "temperature": {
                    "sensors": [
                        {
                            "name": "CPU",
                            "type": "CPU_PACKAGE",
                            "current": {"value": 55.0, "unit": "CELSIUS", "status": "NORMAL"},
                        }
                    ]
                }
            },
            "notifications": {
                "overview": {"unread": HEALTH["notifications_unread"]},
                "warningsAndAlerts": [
                    {"id": "n1", "title": "Disk warning", "importance": "WARNING"}
                ],
            },
        }
    else:
        container = dict.fromkeys(CONTAINER)
        container["id"] = "1:abc"
        container["ports"] = [{"privatePort": None, "publicPort": None, "type": None, "ip": None}]
        disk = dict.fromkeys(DISK)
        disk["id"] = "1:disk"
        data = {"array": None, "upsDevices": None, "notifications": None}
    data.update(
        {
            "docker": {"container": container, "containers": [container]},
            "disk": disk,
            "disks": [disk],
        }
    )
    return data, {
        "get_health_summary": HEALTH
        if kind == "full"
        else {
            "overall": "degraded",
            "reasons": [
                "Array check failed or is unsupported",
                "Temperature check failed or is unsupported",
            ],
            "checks": {
                "array": "failed",
                "ups": "ok",
                "notifications": "ok",
                "temperature": "failed",
            },
            "array_state": None,
            "capacity": {key: {"bytes": None, "human": None} for key in ("total", "used", "free")},
            "disk_count": 0,
            "unhealthy_disks": [],
            "parity_check": None,
            "ups": [],
            "notifications_unread": {},
            "top_alerts": [],
        },
        "list_docker_containers": {"result": [shape_container(container)]},
        "get_docker_container": shape_container_detail(container),
        "list_disks": {"result": [shape_physical_disk(disk)]},
        "get_disk": shape_physical_disk(disk),
    }


async def test_core_tools_advertise_typed_output_schemas(settings_factory):
    with respx.mock:
        respx.post(URL).respond(200, json={"data": {}})
        async with Client(build_server(settings_factory()), raise_exceptions=True) as session:
            schemas = {t.name: t.output_schema for t in (await session.list_tools()).tools}
    for name, _ in TOOLS:
        schema = schemas[name]
        assert schema["type"] == "object"
        assert schema["properties"]
        assert schema.get("additionalProperties") is not True
        if name.startswith("list_"):
            item = schema["properties"]["result"]["items"]["anyOf"][0]
            definition = schema["$defs"][item["$ref"].split("/")[-1]]
            assert definition["properties"]["id"]["type"] == ["string", "null"]
            assert "id" in definition["required"]
        else:
            assert ("overall" if name == "get_health_summary" else "id") in schema["properties"]
    size = schemas["get_disk"]["$defs"]["Size"]
    assert set(size["properties"]) == {"bytes", "human"}
    assert size["properties"]["bytes"]["type"] == ["integer", "null"]
    assert size["properties"]["bytes"]["description"]
    for name in ("list_disks", "get_health_summary"):
        assert schemas[name]["$defs"]["Size"] == size
    assert schemas["get_disk"]["properties"]["size"] == {"$ref": "#/$defs/Size"}
    assert schemas["get_health_summary"]["$defs"]["Capacity"]["properties"]["total"] == {
        "$ref": "#/$defs/Size"
    }
    counts = schemas["get_health_summary"]["$defs"]["NotificationCounts"]
    assert "required" not in counts
    port = schemas["get_docker_container"]["$defs"]["ContainerPort"]
    assert port["properties"]["private"]["type"] == ["integer", "null"]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("kind", ["full", "null"])
async def test_core_outputs_validate_without_changing_values(settings_factory, mode, kind):
    data, expected = _fixture(kind)
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(
            build_server(settings_factory()), raise_exceptions=True, mode=mode
        ) as session:
            for name, arguments in TOOLS:
                result = await session.call_tool(name, arguments)
                assert result.is_error is False
                assert result.structured_content == expected[name]
                assert json.dumps(result.structured_content, sort_keys=True) == json.dumps(
                    expected[name], sort_keys=True
                )
                # One compact text block: the original object/list without the SDK
                # wrapper, null-valued keys dropped (#156).
                original = expected[name].get("result", expected[name])
                assert len(result.content) == 1
                assert result.content[0].text == compact_text(original)


@pytest.mark.parametrize("name", ["list_disks", "list_docker_containers"])
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"disks": None, "docker": None},
        {"disks": [None, {}], "docker": {"containers": [None, {}]}},
    ],
)
async def test_list_outputs_validate_empty_and_null_items(settings_factory, name, data):
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(settings_factory()), raise_exceptions=True) as session:
            result = await session.call_tool(name, {})
            assert result.is_error is False
            assert result.structured_content == {
                "result": [None, None] if data.get("disks") else []
            }


@pytest.mark.parametrize("name,arguments", TOOLS)
async def test_core_output_error_mapping(settings_factory, name, arguments):
    with respx.mock:
        respx.post(URL).respond(200, json={"errors": [{"message": "Backend unavailable"}]})
        async with Client(build_server(settings_factory()), raise_exceptions=True) as session:
            result = await session.call_tool(name, arguments)
            if name == "get_health_summary":
                # Health degrades on failed sub-queries instead of erroring.
                assert result.is_error is False
                assert result.structured_content["overall"] == "degraded"
                assert set(result.structured_content["checks"].values()) == {"failed"}
                return
            assert result.is_error is True
            assert "Backend unavailable" in result.content[0].text
            assert result.structured_content is None


@pytest.mark.parametrize(
    "name,arguments,module,function",
    [
        ("get_health_summary", {}, misc, "fetch_health"),
        ("list_disks", {}, array, "fetch_disks"),
        ("get_disk", {"disk_id": "1:disk"}, array, "fetch_disk"),
        ("list_docker_containers", {}, docker, "fetch_containers"),
        ("get_docker_container", {"identifier": "1:abc"}, docker, "fetch_container"),
    ],
)
async def test_sdk_rejects_output_shaping_drift(
    settings_factory, monkeypatch, name, arguments, module, function, caplog
):
    async def invalid(*args, **kwargs):
        return [{"id": "missing required fields"}] if name.startswith("list_") else {}

    monkeypatch.setattr(module, function, invalid)
    with respx.mock:
        respx.post(URL).respond(200, json={"data": {}})
        async with Client(build_server(settings_factory()), raise_exceptions=True) as session:
            result = await session.call_tool(name, arguments)
            assert result.is_error is True
            assert result.content[0].text == f"Error executing tool {name}"
            assert "ValidationError" in caplog.text


def test_health_summary_nullable_capacity_and_partial_fields():
    from pydantic import TypeAdapter

    from unraid_mcp.types import HealthSummary

    output = summarize_health({"parity_check": {"running": None}}, [], {"unread": {"alert": None}})
    assert TypeAdapter(HealthSummary).validate_python(output) == output


async def test_health_partial_fields_validate_through_sdk(settings_factory):
    data = {
        "array": {"parityCheckStatus": {"running": None}},
        "notifications": {"overview": {"unread": {"alert": None}}},
    }
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(settings_factory()), raise_exceptions=True) as session:
            result = await session.call_tool("get_health_summary", {})
            assert result.is_error is False
            assert result.structured_content["parity_check"] == {"running": None}
            assert result.structured_content["notifications_unread"] == {"alert": None}


@pytest.mark.parametrize(
    "td,extra",
    [
        ("ParityCheck", {"progress": 1, "newField": 2}),
        ("NotificationCounts", {"info": 1, "newField": 2}),
        (
            "DiskPartition",
            {"name": "a", "fsType": "x", "size": {"bytes": 1, "human": "1 B"}, "newField": 2},
        ),
    ],
)
def test_passthrough_typeddicts_reject_unexpected_keys(td, extra):
    from pydantic import TypeAdapter, ValidationError

    from unraid_mcp import types

    with pytest.raises(ValidationError):
        TypeAdapter(getattr(types, td)).validate_python(extra)


@pytest.mark.parametrize(
    "td,query,block",
    [
        ("ParityCheck", "ARRAY_STATUS", r"parityCheckStatus \{([^}]*)\}"),
        ("NotificationCounts", "NOTIFICATIONS_OVERVIEW", r"unread \{([^}]*)\}"),
        ("DiskPartition", "DISK_DETAILS", r"partitions \{([^}]*)\}"),
    ],
)
def test_passthrough_typeddict_keys_match_query_selection(td, query, block):
    import re

    from unraid_mcp import queries, types

    selected = set(re.search(block, getattr(queries, query)).group(1).split())
    assert selected == set(getattr(types, td).__annotations__)


async def test_container_detail_with_sizes_validates_through_sdk(settings_factory):
    container = {
        **CONTAINER,
        "isRebuildReady": False,
        "lanIpPorts": ["192.168.0.2:32400"],
        "iconUrl": "http://x/i.png",
        "mounts": [{"Source": "/a", "Destination": "/b"}],
        "labels": {"k": "v"},
        "tailscaleEnabled": True,
        "tailscaleStatus": {"online": True, "version": "1", "hostname": "h"},
        "sizeRootFs": 2048,
        "sizeRw": 1024,
        "sizeLog": None,
    }
    data = {"docker": {"container": container, "containers": [container]}}
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(settings_factory()), raise_exceptions=True) as session:
            result = await session.call_tool(
                "get_docker_container", {"identifier": "1:abc", "include_sizes": True}
            )
    assert result.is_error is False
    out = result.structured_content
    assert out["size_root_fs"] == {"bytes": 2048, "human": "2.0 KiB"}
    assert out["size_log"] == {"bytes": None, "human": None}
    assert out["mounts"] and out["labels"] == {"k": "v"}
    assert out["tailscale"]["online"] is True
