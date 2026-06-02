"""Tests for pure response-shaping and size helpers."""

from __future__ import annotations

import pytest

from unraid_mcp.formatting import (
    array_disk_health,
    human_size,
    kib_to_bytes,
    shape_array_status,
    shape_physical_disk,
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
    ],
)
def test_human_size(num, expected):
    assert human_size(num) == expected


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("1024", 1024 * 1024), (2, 2048)])
def test_kib_to_bytes(value, expected):
    assert kib_to_bytes(value) == expected


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
            "parities": [{"name": "parity", "type": "PARITY", "status": "DISK_OK", "size": "1048576"}],
            "disks": [
                {
                    "name": "disk1", "device": "sdb", "type": "DATA", "status": "DISK_OK",
                    "temp": 35, "fsType": "xfs", "size": "1048576", "fsUsed": "524288",
                    "fsFree": "524288", "numReads": "10", "numWrites": "5", "numErrors": "0",
                    "warning": 0, "critical": 0, "color": "green-on",
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
        "id": "1:abc", "device": "sdb", "name": "WDC", "vendor": "WD", "type": "HD",
        "size": 2_000_000_000_000, "interfaceType": "SATA", "smartStatus": "OK",
        "temperature": 36.0, "isSpinning": True, "serialNum": "X",
    }
    out = shape_physical_disk(raw)
    assert out["smart_status"] == "OK"
    assert out["spinning"] is True
    assert out["size"]["bytes"] == 2_000_000_000_000
    assert out["size"]["human"].endswith("TiB")


@pytest.mark.parametrize(
    "status,warning,critical,expected",
    [
        ("DISK_OK", 0, 0, "healthy"),
        ("DISK_OK", 1, 0, "warning"),
        ("DISK_OK", 0, 1, "critical"),
        ("DISK_DSBL", 0, 0, "failed"),
        ("DISK_INVALID", 0, 0, "failed"),
        ("DISK_NP", 0, 0, "missing"),
        ("DISK_NEW", 0, 0, "new"),
        (None, 0, 0, "unknown"),
    ],
)
def test_array_disk_health(status, warning, critical, expected):
    assert array_disk_health(status, warning, critical) == expected
