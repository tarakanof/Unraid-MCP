"""Tests for pure response-shaping and size helpers."""

from __future__ import annotations

import pytest

from unraid_mcp.formatting import (
    array_disk_health,
    human_size,
    kib_to_bytes,
    sanitize_control,
    shape_array_status,
    shape_container_stats,
    shape_flash,
    shape_metrics,
    shape_mutation_result,
    shape_physical_disk,
    shape_shares,
    shape_system_time,
)


@pytest.mark.parametrize(
    "num,expected",
    [
        (None, None),
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**3, "1.0 GiB"),
        (3 * 1024**4, "3.0 TiB"),
        (1024**5, "1.0 PiB"),
        (1024**6, "1.0 EiB"),
    ],
)
def test_human_size(num, expected):
    assert human_size(num) == expected


@pytest.mark.parametrize(
    "value,expected", [(None, None), ("", None), ("1024", 1024 * 1024), (2, 2048)]
)
def test_kib_to_bytes(value, expected):
    assert kib_to_bytes(value) == expected


def test_shape_metrics_shapes_cpu_memory_temperature():
    raw = {
        "metrics": {
            "cpu": {
                "percentTotal": 12.345,
                "cpus": [{"percentTotal": 5.05}, {"percentTotal": 19.999}],
            },
            "memory": {
                "total": 17179869184,
                "used": 8589934592,
                "free": 8589934592,
                "available": 8589934592,
                "percentTotal": 50.0,
                "swapTotal": 4294967296,
                "swapUsed": 0,
                "swapFree": 4294967296,
                "percentSwapTotal": 0.0,
            },
            "temperature": {
                "summary": {"average": 42.5, "warningCount": 0, "criticalCount": 0},
                "sensors": [{"name": "CPU", "current": {"value": 45.0, "unit": "C"}}],
            },
        }
    }
    out = shape_metrics(raw)
    assert out["cpu"] == {"percent_total": 12.3, "per_core": [5.0, 20.0]}
    assert out["memory"]["total"] == {"bytes": 17179869184, "human": "16.0 GiB"}
    assert out["memory"]["swap_free"] == {"bytes": 4294967296, "human": "4.0 GiB"}
    assert out["memory"]["percent_total"] == 50.0
    assert out["temperature"]["summary"]["average"] == 42.5
    assert out["temperature"]["sensors"] == [
        {"name": "CPU", "current": {"value": 45.0, "unit": "C"}}
    ]


def test_shape_metrics_partial_response_omits_missing_temperature():
    raw = {
        "metrics": {
            "cpu": {"percentTotal": 1.0, "cpus": []},
            "memory": {"total": 1024, "used": 512, "free": 512, "available": 512},
            "temperature": None,
        }
    }
    out = shape_metrics(raw)
    assert "cpu" in out
    assert "memory" in out
    assert "temperature" not in out


def test_shape_system_time_filters_empty_ntp_slots():
    raw = {
        "systemTime": {
            "currentTime": "2026-07-03T12:00:00Z",
            "timeZone": "UTC",
            "useNtp": True,
            "ntpServers": ["0.pool.ntp.org", "", ""],
        }
    }
    assert shape_system_time(raw) == {
        "current_time": "2026-07-03T12:00:00Z",
        "time_zone": "UTC",
        "use_ntp": True,
        "ntp_servers": ["0.pool.ntp.org"],
    }


def test_shape_system_time_handles_empty():
    assert shape_system_time(None) == {
        "current_time": None,
        "time_zone": None,
        "use_ntp": None,
        "ntp_servers": [],
    }


def test_shape_flash():
    raw = {"flash": {"guid": "abc-123", "vendor": "SanDisk", "product": "Cruzer"}}
    assert shape_flash(raw) == {"guid": "abc-123", "vendor": "SanDisk", "product": "Cruzer"}


def test_shape_flash_handles_empty():
    assert shape_flash(None) == {"guid": None, "vendor": None, "product": None}


def test_shape_shares_includes_and_omits_extra_fields():
    raw = {
        "shares": [
            {
                "name": "secure",
                "include": ["disk1"],
                "exclude": [],
                "splitLevel": "2",
                "floor": None,
                "luksStatus": "ENCRYPTED",
            }
        ]
    }
    out = shape_shares(raw)[0]
    assert out["include"] == ["disk1"]
    assert out["split_level"] == "2"
    assert out["encryption_status"] == "ENCRYPTED"
    assert "exclude" not in out
    assert "floor" not in out


def test_shape_array_status_converts_capacity_and_disks():
    raw = {
        "array": {
            "state": "STARTED",
            "capacity": {
                "kilobytes": {"total": "1048576", "used": "524288", "free": "524288"},
                "disks": {"total": "3", "used": "2", "free": "1"},
            },
            "parityCheckStatus": {"running": False, "status": "COMPLETED"},
            "boot": None,
            "parities": [
                {"name": "parity", "type": "PARITY", "status": "DISK_OK", "size": "1048576"}
            ],
            "disks": [
                {
                    "name": "disk1",
                    "device": "sdb",
                    "type": "DATA",
                    "status": "DISK_OK",
                    "temp": 35,
                    "fsType": "xfs",
                    "size": "1048576",
                    "fsUsed": "524288",
                    "fsFree": "524288",
                    "numReads": "10",
                    "numWrites": "5",
                    "numErrors": "0",
                    "warning": 0,
                    "critical": 0,
                    "color": "green-on",
                }
            ],
            "caches": [],
        }
    }
    out = shape_array_status(raw)
    assert out["state"] == "STARTED"
    # 1048576 KiB == 1 GiB
    assert out["capacity"]["total"]["human"] == "1.0 GiB"
    assert out["capacity"]["total"]["bytes"] == 1048576 * 1024
    assert out["disk_slots"] == {"total": "3", "used": "2", "free": "1"}
    d = out["data_disks"][0]
    assert d["name"] == "disk1"
    assert d["temp_c"] == 35
    assert d["size"]["human"] == "1.0 GiB"
    assert d["health"] == "healthy"
    assert out["boot"] is None
    assert out["parities"][0]["name"] == "parity"


def test_shape_array_status_handles_empty():
    assert shape_array_status({})["state"] is None
    assert shape_array_status({"array": {}})["data_disks"] == []


def test_shape_physical_disk_size_is_bytes():
    raw = {
        "id": "1:abc",
        "device": "sdb",
        "name": "WDC",
        "vendor": "WD",
        "type": "HD",
        "size": 2_000_000_000_000,
        "interfaceType": "SATA",
        "smartStatus": "OK",
        "temperature": 36.0,
        "isSpinning": True,
        "serialNum": "X",
    }
    out = shape_physical_disk(raw)
    assert out["smart_status"] == "OK"
    assert out["spinning"] is True
    assert out["size"]["bytes"] == 2_000_000_000_000
    assert out["size"]["human"].endswith("TiB")


@pytest.mark.parametrize(
    "raw,expected",
    [
        # VM and parity mutations resolve to a bare Boolean payload.
        ({"vm": {"start": True}}, {"ok": True}),
        ({"vm": {"forceStop": False}}, {"ok": False}),
        ({"parityCheck": {"start": True}}, {"ok": True}),
        ({"parityCheck": {"pause": True}}, {"ok": True}),
        # Empty / missing envelopes degrade to a success flag, not a bare {}.
        ({"docker": {}}, {"ok": True}),
        (None, {"ok": True}),
        ({}, {"ok": True}),
    ],
)
def test_shape_mutation_result_flattens_to_ok(raw, expected):
    assert shape_mutation_result(raw) == expected


def test_shape_mutation_result_keeps_object_payload():
    # Docker start/stop and notification archive/unread return an object — the
    # GraphQL wrapper keys are peeled but the payload fields are preserved.
    raw = {"docker": {"start": {"id": "1:a", "names": ["/plex"], "state": "RUNNING"}}}
    assert shape_mutation_result(raw) == {
        "id": "1:a",
        "names": ["/plex"],
        "state": "RUNNING",
    }
    note = {"archiveNotification": {"id": "n1", "title": "Disk hot", "importance": "ALERT"}}
    assert shape_mutation_result(note) == {
        "id": "n1",
        "title": "Disk hot",
        "importance": "ALERT",
    }


def test_shape_mutation_result_normalizes_array_capacity():
    # start_array returns capacity in KiB; it must be normalised to {bytes, human}
    # so a mutation result reads identically to get_array_status.
    raw = {
        "array": {
            "setState": {
                "state": "STARTED",
                "capacity": {"kilobytes": {"total": "1048576", "used": "524288", "free": "524288"}},
            }
        }
    }
    out = shape_mutation_result(raw)
    assert out["state"] == "STARTED"
    assert out["capacity"]["total"] == {"bytes": 1048576 * 1024, "human": "1.0 GiB"}
    assert out["capacity"]["free"]["bytes"] == 524288 * 1024


def test_shape_mutation_result_array_stop_state_only():
    # stop_array returns just {state}; peeling stops at the scalar value, so the
    # single state field survives unwrapped (and there's no capacity to normalize).
    assert shape_mutation_result({"array": {"setState": {"state": "STOPPED"}}}) == {
        "state": "STOPPED"
    }


def test_shape_mutation_result_keeps_multifield_overview():
    # archive_all / delete_notification return {unread, archive} counts — a
    # two-key dict, so peeling stops there and both buckets survive.
    raw = {
        "archiveAll": {
            "unread": {"info": 0, "warning": 0, "alert": 0, "total": 0},
            "archive": {"info": 1, "warning": 2, "alert": 0, "total": 3},
        }
    }
    out = shape_mutation_result(raw)
    assert out["unread"]["total"] == 0
    assert out["archive"]["total"] == 3


@pytest.mark.parametrize(
    "status,warning,critical,expected",
    [
        ("DISK_OK", 0, 0, "healthy"),
        ("DISK_OK", 1, 0, "warning"),
        ("DISK_OK", 0, 1, "critical"),
        ("DISK_DSBL", 0, 0, "failed"),
        ("DISK_INVALID", 0, 0, "failed"),
        ("DISK_NP", 0, 0, "empty"),
        ("DISK_NP_MISSING", 0, 0, "missing"),
        ("DISK_NP_DSBL", 0, 0, "failed"),
        ("DISK_NEW", 0, 0, "new"),
        (None, 0, 0, "unknown"),
    ],
)
def test_array_disk_health(status, warning, critical, expected):
    assert array_disk_health(status, warning, critical) == expected


# ── Control-char sanitization + container-stats shaping (#65) ─────────────────


@pytest.mark.parametrize(
    "polluted,clean",
    [
        # The exact shape from #27: ANSI cursor-home CSI embedded in the id.
        ("docker:abc123\x1b[Hdef", "docker:abc123def"),
        ("docker:\x1b[Jabc123", "docker:abc123"),
        ("docker:\x1b[2Jabc", "docker:abc"),  # CSI with a numeric parameter
        ("docker:a\x00b\x07c\x1fd", "docker:abcd"),  # bare C0 controls
        ("docker:clean", "docker:clean"),  # already clean → unchanged
    ],
)
def test_sanitize_control_strips_ansi_and_c0(polluted, clean):
    assert sanitize_control(polluted) == clean


def test_sanitize_control_passes_non_strings_through():
    assert sanitize_control(None) is None
    assert sanitize_control(42) == 42


def test_sanitize_control_is_idempotent():
    once = sanitize_control("docker:abc\x1b[Hdef")
    assert sanitize_control(once) == once


def test_shape_container_stats_cleans_id_and_passes_strings():
    events = [
        {
            "dockerContainerStats": {
                "id": "docker:first\x1b[H",  # polluted first-of-cycle id
                "cpuPercent": 12.5,
                "memPercent": 3.2,
                "memUsage": "65.56MiB / 31.25GiB",
                "netIO": "1.2kB / 3.4kB",
                "blockIO": "0B / 8.19kB",
            }
        },
        {
            "dockerContainerStats": {
                "id": "docker:second",
                "cpuPercent": 0.0,
                "memPercent": 1.0,
                "memUsage": "10MiB / 1GiB",
                "netIO": "0B / 0B",
                "blockIO": "0B / 0B",
            }
        },
    ]
    out = shape_container_stats(events)
    assert out[0]["id"] == "docker:first"  # control chars stripped
    assert out[0]["cpu_percent"] == 12.5
    assert out[0]["mem_percent"] == 3.2
    # Pre-formatted composite strings pass through verbatim — no {bytes, human}.
    assert out[0]["mem_usage"] == "65.56MiB / 31.25GiB"
    assert out[0]["net_io"] == "1.2kB / 3.4kB"
    assert out[0]["block_io"] == "0B / 8.19kB"
    assert out[1]["id"] == "docker:second"
    # No size-shaped dict slipped in (would carry a "human"/"bytes" pair).
    for entry in out:
        for value in entry.values():
            assert not (isinstance(value, dict) and "human" in value)


def test_shape_container_stats_empty():
    assert shape_container_stats([]) == []
    assert shape_container_stats(None) == []
