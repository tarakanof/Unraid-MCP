"""Pure functions that shape raw Unraid GraphQL responses into concise,
JSON-friendly dicts for MCP tool output.

Kept free of I/O so they are trivially unit-testable. Two unit conventions
from the schema are normalised here so callers never have to remember them:

  * ``ArrayDisk``/``Share`` sizes are **KiB** → use :func:`kib_to_bytes`.
  * physical ``Disk.size`` is **bytes** already.

Every size field is emitted as ``{"bytes": int|None, "human": str|None}``.
"""

from __future__ import annotations

from typing import Any

_FAILED_STATUSES = {"DISK_DSBL", "DISK_INVALID", "DISK_WRONG", "DISK_DSBL_NEW", "DISK_NP_DSBL"}
# DISK_NP means "no device present" - an empty/unassigned array slot, which is a
# normal, healthy state when the array has spare slots. DISK_NP_MISSING means a
# disk that *is* assigned to the array is not present - a real problem.
_EMPTY_STATUSES = {"DISK_NP"}
_MISSING_STATUSES = {"DISK_NP_MISSING"}
_NEW_STATUSES = {"DISK_NEW"}


def human_size(num_bytes: float | int | None) -> str | None:
    """Format a byte count as a human-readable binary size."""
    if num_bytes is None:
        return None
    value = float(num_bytes)
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} ZiB"


def kib_to_bytes(value: Any) -> int | None:
    """Convert a KiB count (string or number) to bytes; ``None`` if unparseable."""
    if value is None or value == "":
        return None
    try:
        return int(value) * 1024
    except (TypeError, ValueError):
        return None


def _size_from_kib(value: Any) -> dict[str, Any]:
    b = kib_to_bytes(value)
    return {"bytes": b, "human": human_size(b)}


def _size_from_bytes(value: Any) -> dict[str, Any]:
    try:
        b = int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        b = None
    return {"bytes": b, "human": human_size(b)}


def array_disk_health(status: str | None, warning: Any = 0, critical: Any = 0) -> str:
    """Map an ``ArrayDiskStatus`` (+ warning/critical flags) to a coarse health word."""
    if not status:
        return "unknown"
    if status == "DISK_OK":
        if critical:
            return "critical"
        if warning:
            return "warning"
        return "healthy"
    if status in _FAILED_STATUSES:
        return "failed"
    if status in _EMPTY_STATUSES:
        return "empty"
    if status in _MISSING_STATUSES:
        return "missing"
    if status in _NEW_STATUSES:
        return "new"
    return "unknown"


def _shape_array_disk(d: dict | None) -> dict[str, Any] | None:
    if not d:
        return None
    return {
        "name": d.get("name"),
        "device": d.get("device"),
        "type": d.get("type"),
        "status": d.get("status"),
        "health": array_disk_health(d.get("status"), d.get("warning"), d.get("critical")),
        "temp_c": d.get("temp"),
        "fs_type": d.get("fsType"),
        "size": _size_from_kib(d.get("size")),
        "fs_used": _size_from_kib(d.get("fsUsed")),
        "fs_free": _size_from_kib(d.get("fsFree")),
        "reads": d.get("numReads"),
        "writes": d.get("numWrites"),
        "errors": d.get("numErrors"),
        "color": d.get("color"),
    }


def shape_array_status(data: dict | None) -> dict[str, Any]:
    array = (data or {}).get("array") or {}
    capacity = array.get("capacity") or {}
    kib = capacity.get("kilobytes") or {}
    return {
        "state": array.get("state"),
        "capacity": {
            "total": _size_from_kib(kib.get("total")),
            "used": _size_from_kib(kib.get("used")),
            "free": _size_from_kib(kib.get("free")),
        },
        "disk_slots": capacity.get("disks"),
        "parity_check": array.get("parityCheckStatus"),
        "parities": [_shape_array_disk(d) for d in (array.get("parities") or [])],
        "data_disks": [_shape_array_disk(d) for d in (array.get("disks") or [])],
        "caches": [_shape_array_disk(d) for d in (array.get("caches") or [])],
        "boot": _shape_array_disk(array.get("boot")),
    }


def shape_physical_disk(d: dict | None) -> dict[str, Any] | None:
    if not d:
        return None
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "device": d.get("device"),
        "vendor": d.get("vendor"),
        "type": d.get("type"),
        "serial": d.get("serialNum"),
        "interface": d.get("interfaceType"),
        "smart_status": d.get("smartStatus"),
        "temp_c": d.get("temperature"),
        "spinning": d.get("isSpinning"),
        "size": _size_from_bytes(d.get("size")),
        "firmware": d.get("firmwareRevision"),
        "partitions": d.get("partitions"),
    }


def shape_physical_disks(data: dict | None) -> list[dict[str, Any]]:
    return [shape_physical_disk(d) for d in ((data or {}).get("disks") or [])]


def shape_system_info(data: dict | None) -> dict[str, Any]:
    return (data or {}).get("info") or {}


def shape_metrics(data: dict | None) -> dict[str, Any]:
    """Shape the ``metrics`` live-utilization snapshot.

    ``cpu``/``memory``/``temperature`` are each independently nullable on the
    upstream ``Metrics`` type, so a partial response (e.g. no temperature
    sensors configured) still yields the fields that ARE present — callers
    must not assume all three keys exist.
    """
    metrics = (data or {}).get("metrics") or {}
    out: dict[str, Any] = {}

    cpu = metrics.get("cpu")
    if cpu is not None:
        out["cpu"] = {
            "percent_total": round(cpu.get("percentTotal"), 1)
            if cpu.get("percentTotal") is not None
            else None,
            "per_core": [
                round(c.get("percentTotal"), 1) if c.get("percentTotal") is not None else None
                for c in (cpu.get("cpus") or [])
            ],
        }

    memory = metrics.get("memory")
    if memory is not None:
        out["memory"] = {
            "total": _size_from_bytes(memory.get("total")),
            "used": _size_from_bytes(memory.get("used")),
            "free": _size_from_bytes(memory.get("free")),
            "available": _size_from_bytes(memory.get("available")),
            "percent_total": memory.get("percentTotal"),
            "swap_total": _size_from_bytes(memory.get("swapTotal")),
            "swap_used": _size_from_bytes(memory.get("swapUsed")),
            "swap_free": _size_from_bytes(memory.get("swapFree")),
            "percent_swap_total": memory.get("percentSwapTotal"),
        }

    temperature = metrics.get("temperature")
    if temperature is not None:
        summary = temperature.get("summary") or {}
        out["temperature"] = {
            "summary": {
                "average": summary.get("average"),
                "warning_count": summary.get("warningCount"),
                "critical_count": summary.get("criticalCount"),
            },
            "sensors": [
                {
                    "name": s.get("name"),
                    "current": {
                        "value": (s.get("current") or {}).get("value"),
                        "unit": (s.get("current") or {}).get("unit"),
                    },
                }
                for s in (temperature.get("sensors") or [])
            ],
        }

    return out


def shape_services(data: dict | None) -> list[dict[str, Any]]:
    services = (data or {}).get("services") or []
    out = []
    for s in services:
        uptime = s.get("uptime") or {}
        out.append(
            {
                "name": s.get("name"),
                "online": s.get("online"),
                "uptime": uptime.get("timestamp"),
                "version": s.get("version"),
            }
        )
    return out


def shape_container(c: dict | None) -> dict[str, Any] | None:
    if not c:
        return None
    names = c.get("names") or []
    return {
        "id": c.get("id"),
        "name": names[0].lstrip("/") if names else None,
        "names": names,
        "image": c.get("image"),
        "state": c.get("state"),
        "status": c.get("status"),
        "auto_start": c.get("autoStart"),
        "ports": [
            {
                "private": p.get("privatePort"),
                "public": p.get("publicPort"),
                "type": p.get("type"),
                "ip": p.get("ip"),
            }
            for p in (c.get("ports") or [])
        ],
    }


def shape_containers(data: dict | None) -> list[dict[str, Any]]:
    docker = (data or {}).get("docker") or {}
    return [shape_container(c) for c in (docker.get("containers") or [])]


def shape_docker_networks(data: dict | None) -> list[dict[str, Any]]:
    docker = (data or {}).get("docker") or {}
    return docker.get("networks") or []


_LOG_LINE_MAX_CHARS = 2000
_TRUNCATION_MARKER = "… [truncated]"


def _shape_log_line(line: dict | None) -> dict[str, Any]:
    line = line or {}
    message = line.get("message") or ""
    truncated = len(message) > _LOG_LINE_MAX_CHARS
    if truncated:
        message = message[:_LOG_LINE_MAX_CHARS] + _TRUNCATION_MARKER
    return {"timestamp": line.get("timestamp"), "message": message, "truncated": truncated}


def shape_container_logs(data: dict | None) -> dict[str, Any]:
    docker = (data or {}).get("docker") or {}
    logs = docker.get("logs") or {}
    lines = [_shape_log_line(line) for line in (logs.get("lines") or [])]
    return {
        "container_id": logs.get("containerId"),
        "lines": lines,
        "cursor": logs.get("cursor"),
        "truncated": any(line["truncated"] for line in lines),
    }


def shape_log_files(data: dict | None) -> list[dict[str, Any]]:
    return [
        {
            "name": f.get("name"),
            "path": f.get("path"),
            "size": _size_from_bytes(f.get("size")),
            "modified_at": f.get("modifiedAt"),
        }
        for f in (data or {}).get("logFiles") or []
    ]


def shape_log_file(data: dict | None) -> dict[str, Any]:
    f = (data or {}).get("logFile") or {}
    return {
        "path": f.get("path"),
        "content": f.get("content"),
        "total_lines": f.get("totalLines"),
        "start_line": f.get("startLine"),
    }


def shape_docker_update_statuses(data: dict | None) -> list[dict[str, Any]]:
    docker = (data or {}).get("docker") or {}
    return [
        {"name": item.get("name"), "update_status": item.get("updateStatus")}
        for item in (docker.get("containerUpdateStatuses") or [])
    ]


def shape_vms(data: dict | None) -> list[dict[str, Any]]:
    vms = (data or {}).get("vms") or {}
    if not isinstance(vms, dict):
        return []
    # `domains` is canonical; `domain` is a legacy alias kept for older builds.
    domains = vms.get("domains") or vms.get("domain") or []
    return [{"id": d.get("id"), "name": d.get("name"), "state": d.get("state")} for d in domains]


def shape_shares(data: dict | None) -> list[dict[str, Any]]:
    shares = []
    for s in (data or {}).get("shares") or []:
        share = {
            "name": s.get("name"),
            "comment": s.get("comment"),
            "free": _size_from_kib(s.get("free")),
            "used": _size_from_kib(s.get("used")),
            "size": _size_from_kib(s.get("size")),
            "allocator": s.get("allocator"),
            "cache": s.get("cache"),
        }
        # Extra fields are compact-optional: omit rather than emit None/empty
        # noise for shares that don't set include/exclude/split-level/etc.
        extras = {
            "include": s.get("include") or None,
            "exclude": s.get("exclude") or None,
            "split_level": s.get("splitLevel") or None,
            "floor": s.get("floor") or None,
            "encryption_status": s.get("luksStatus") or None,
        }
        share.update({k: v for k, v in extras.items() if v is not None})
        shares.append(share)
    return shares


def shape_system_time(data: dict | None) -> dict[str, Any]:
    time_info = (data or {}).get("systemTime") or {}
    return {
        "current_time": time_info.get("currentTime"),
        "time_zone": time_info.get("timeZone"),
        "use_ntp": time_info.get("useNtp"),
        # Empty strings indicate unused NTP server slots per the schema note.
        "ntp_servers": [s for s in (time_info.get("ntpServers") or []) if s],
    }


def shape_flash(data: dict | None) -> dict[str, Any]:
    flash = (data or {}).get("flash") or {}
    return {
        "guid": flash.get("guid"),
        "vendor": flash.get("vendor"),
        "product": flash.get("product"),
    }


def shape_notifications(data: dict | None) -> list[dict[str, Any]]:
    notifications = (data or {}).get("notifications") or {}
    return notifications.get("list") or []


def shape_notifications_overview(data: dict | None) -> dict[str, Any]:
    notifications = (data or {}).get("notifications") or {}
    return notifications.get("overview") or {}


def shape_ups(data: dict | None) -> list[dict[str, Any]]:
    return (data or {}).get("upsDevices") or []


def shape_network_interfaces(data: dict | None) -> list[dict[str, Any]]:
    return (data or {}).get("networkInterfaces") or []


def shape_me(data: dict | None) -> dict[str, Any]:
    return (data or {}).get("me") or {}


def shape_connect_status(data: dict | None) -> dict[str, Any]:
    data = data or {}
    return {"registration": data.get("registration"), "remote_access": data.get("remoteAccess")}


def _normalize_capacity(obj: dict[str, Any]) -> dict[str, Any]:
    """Normalise a ``capacity { kilobytes { total used free } }`` block to the
    ``{bytes, human}`` shape used everywhere else, so a mutation result reads the
    same as :func:`shape_array_status` rather than leaking raw KiB integers."""
    cap = obj.get("capacity")
    if isinstance(cap, dict) and isinstance(cap.get("kilobytes"), dict):
        kib = cap["kilobytes"]
        return {
            **obj,
            "capacity": {
                "total": _size_from_kib(kib.get("total")),
                "used": _size_from_kib(kib.get("used")),
                "free": _size_from_kib(kib.get("free")),
            },
        }
    return obj


def shape_mutation_result(data: dict | None) -> dict[str, Any]:
    """Flatten a GraphQL mutation response into a concise, agent-friendly result.

    Unraid nests mutation payloads under the operation's root field (and often a
    sub-field), e.g. ``{"docker": {"start": {...}}}`` or ``{"vm": {"start": true}}``.
    Those wrapper keys are pure noise to an agent and inflate its context window, so
    we peel them down to the payload and normalise it — matching the read path:

      * a Boolean payload        → ``{"ok": <bool>}`` (clear success/failure flag)
      * an empty object          → ``{"ok": true}``
      * an object with capacity  → capacity normalised to ``{bytes, human}``
      * any other object         → returned as-is (already field-filtered by the query)
    """
    payload: Any = data or {}
    # Peel single-key wrapper dicts (GraphQL root + mutation field) until we reach a
    # Boolean, an empty/multi-field object, or a non-dict/bool leaf.
    while isinstance(payload, dict) and len(payload) == 1:
        inner = next(iter(payload.values()))
        if not isinstance(inner, (dict, bool)):
            break
        payload = inner
    if isinstance(payload, bool):
        return {"ok": payload}
    if isinstance(payload, dict):
        return {"ok": True} if not payload else _normalize_capacity(payload)
    return {"ok": True, "result": payload}


def summarize_health(
    array_out: dict[str, Any],
    ups_list: list[dict[str, Any]],
    notifications_overview: dict[str, Any],
) -> dict[str, Any]:
    """Compose a compact, triage-friendly health roll-up from the shaped parts."""
    disks = [
        d
        for d in (
            (array_out.get("parities") or [])
            + (array_out.get("data_disks") or [])
            + (array_out.get("caches") or [])
        )
        # empty array slots (no device assigned) are not real disks - exclude them
        # from the disk count entirely so it reflects actual installed devices.
        if d and d.get("health") != "empty"
    ]
    unhealthy = [d for d in disks if d and d.get("health") not in ("healthy", None)]
    unread = (notifications_overview or {}).get("unread") or {}
    has_attention = bool(unhealthy or unread.get("alert") or unread.get("warning"))
    return {
        "overall": "attention" if has_attention else "ok",
        "array_state": array_out.get("state"),
        "capacity": array_out.get("capacity"),
        "disk_count": len(disks),
        "unhealthy_disks": [
            {"name": d.get("name"), "health": d.get("health"), "status": d.get("status")}
            for d in unhealthy
        ],
        "parity_check": array_out.get("parity_check"),
        "ups": [
            {
                "name": u.get("name"),
                "status": u.get("status"),
                "battery_pct": (u.get("battery") or {}).get("chargeLevel"),
            }
            for u in (ups_list or [])
        ],
        "notifications_unread": unread,
    }
