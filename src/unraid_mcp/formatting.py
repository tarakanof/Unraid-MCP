"""Pure functions that shape raw Unraid GraphQL responses into concise,
JSON-friendly dicts for MCP tool output.

Kept free of I/O so they are trivially unit-testable. Two unit conventions
from the schema are normalised here so callers never have to remember them:

  * ``ArrayDisk``/``Share`` sizes are **KiB** → use :func:`kib_to_bytes`.
  * physical ``Disk.size`` is **bytes** already.

Every size field is emitted as ``{"bytes": int|None, "human": str|None}``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import UnraidServerError
from .types import ArrayDisk, Container, Disk, HealthSummary, Size

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


def _size_from_kib(value: Any) -> Size:
    b = kib_to_bytes(value)
    return {"bytes": b, "human": human_size(b)}


def _size_from_bytes(value: Any) -> Size:
    try:
        b = int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        b = None
    return {"bytes": b, "human": human_size(b)}


def array_disk_health(status: str | None, warning: Any = 0, critical: Any = 0) -> str:
    """Map disk status to health. Space thresholds are not alarm flags.

    The warning/critical arguments are retained for compatibility and ignored.
    """
    if not status:
        return "unknown"
    if status == "DISK_OK":
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


def _shape_array_disk(d: dict | None) -> ArrayDisk | None:
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
        "spinning": d.get("isSpinning"),
        "format": d.get("format"),
        "transport": d.get("transport"),
        "exportable": d.get("exportable"),
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
        # `None` (not []) when the API build predates `bootDevices`, so callers
        # can tell "unsupported" apart from "no boot devices".
        "boot_devices": (
            [_shape_array_disk(d) for d in array["bootDevices"]]
            if array.get("bootDevices") is not None
            else None
        ),
    }


def shape_physical_disk(d: dict | None) -> Disk | None:
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
        "partitions": [
            {**p, "size": _size_from_bytes(p.get("size"))}
            for p in (d.get("partitions") or [])
            if isinstance(p, dict)
        ],
    }


def shape_physical_disks(data: dict | None) -> list[Disk | None]:
    return [shape_physical_disk(d) for d in ((data or {}).get("disks") or [])]


def shape_system_info(data: dict | None) -> dict[str, Any]:
    return (data or {}).get("info") or {}


def _shape_reading(sensor: dict, *extra: str) -> dict[str, Any]:
    current = sensor.get("current") or {}
    return {
        **{k: sensor.get(k) for k in extra},
        "value": current.get("value"),
        "unit": current.get("unit"),
    }


def _temp_level(value: Any, warning: Any, critical: Any) -> str | None:
    """``critical``/``warning`` when the reading is at or above that threshold,
    ``normal`` when thresholds exist but aren't reached, else ``None`` (no
    threshold data, e.g. an older API build)."""
    if value is None:
        return None
    if critical is not None and value >= critical:
        return "critical"
    if warning is not None and value >= warning:
        return "warning"
    return "normal" if (warning is not None or critical is not None) else None


def _shape_sensor(s: dict) -> dict[str, Any]:
    current = s.get("current") or {}
    out = {"name": s.get("name"), "type": s.get("type"), "location": s.get("location")}
    out["current"] = {"value": current.get("value"), "unit": current.get("unit")}
    for key in ("min", "max"):
        reading = s.get(key)
        out[key] = {"value": reading.get("value"), "unit": reading.get("unit")} if reading else None
    out["warning"] = s.get("warning")
    out["critical"] = s.get("critical")
    # Upstream's own status is authoritative; derive from thresholds only when
    # it is absent or UNKNOWN (older API builds).
    status = current.get("status")
    if status in ("NORMAL", "WARNING", "CRITICAL"):
        out["level"] = status.lower()
    else:
        out["level"] = _temp_level(current.get("value"), s.get("warning"), s.get("critical"))
    return out


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
        hottest = summary.get("hottest")
        out["temperature"] = {
            "summary": {
                "average": summary.get("average"),
                "warning_count": summary.get("warningCount"),
                "critical_count": summary.get("criticalCount"),
                "hottest": _shape_reading(hottest, "name") if hottest else None,
            },
            "sensors": [_shape_sensor(s) for s in (temperature.get("sensors") or [])],
        }

    return out


def _rate(value: Any) -> dict[str, Any]:
    """Format a bytes-per-second rate as ``{"bytes_per_sec", "human"}``."""
    if value is None:
        return {"bytes_per_sec": None, "human": None}
    human = human_size(value)
    return {"bytes_per_sec": round(float(value), 1), "human": f"{human}/s"}


def _count(value: Any) -> int | None:
    """BigInt counters may arrive as strings; coerce to int, None if unparseable."""
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def shape_metrics_network(data: dict | None) -> list[dict[str, Any]]:
    """Shape ``metrics.network`` per-interface throughput.

    Rates are ``{bytes_per_sec, human}``, totals ``{bytes, human}``, error and
    drop counters plain ints (``None`` when the API returns null).
    """
    network = ((data or {}).get("metrics") or {}).get("network") or []
    return [
        {
            "name": n.get("name"),
            "operstate": n.get("operstate"),
            "rx": _rate(n.get("rxSec")),
            "tx": _rate(n.get("txSec")),
            "utilization_percent": n.get("utilizationPercent"),
            "bytes_received": _size_from_bytes(n.get("bytesReceived")),
            "bytes_sent": _size_from_bytes(n.get("bytesSent")),
            "receive_errors": _count(n.get("receiveErrors")),
            "transmit_errors": _count(n.get("transmitErrors")),
            "receive_dropped": _count(n.get("receiveDropped")),
            "transmit_dropped": _count(n.get("transmitDropped")),
            "last_updated": n.get("lastUpdated"),
        }
        for n in network
        if n
    ]


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


# Cap for the serialized ``labels`` JSON on the single-container view. Compose /
# template-heavy containers can carry dozens of labels; past this the map is
# dropped (``labels_truncated: true``) to protect the agent's context window.
MAX_LABELS_CHARS = 4096


def _shape_labels(labels: Any) -> tuple[Any, bool]:
    if labels is None:
        return None, False
    if len(json.dumps(labels, default=str)) > MAX_LABELS_CHARS:
        return None, True
    return labels, False


def shape_container(c: dict | None) -> Container | None:
    """Compact list-view shape (cheap fields only). Newer-API fields are ``None``
    when the connected build predates them."""
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
        "auto_start_order": c.get("autoStartOrder"),
        "update_available": c.get("isUpdateAvailable"),
        "orphaned": c.get("isOrphaned"),
        "web_ui_url": c.get("webUiUrl"),
        "network_mode": (c.get("hostConfig") or {}).get("networkMode"),
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


def shape_container_detail(c: dict | None) -> Container | None:
    """Single-container shape: the list view plus mounts, labels, links
    and Tailscale. Sizes are NOT included (see :func:`shape_container_sizes`)."""
    out = shape_container(c)
    if out is None or c is None:
        return out
    labels, labels_truncated = _shape_labels(c.get("labels"))
    ts = c.get("tailscaleStatus")
    out.update(
        {
            "rebuild_ready": c.get("isRebuildReady"),
            "lan_ip_ports": c.get("lanIpPorts") or [],
            "icon_url": c.get("iconUrl"),
            "project_url": c.get("projectUrl"),
            "support_url": c.get("supportUrl"),
            "template_path": c.get("templatePath"),
            "auto_start_wait": c.get("autoStartWait"),
            "mounts": c.get("mounts") or [],
            "labels": labels,
            "labels_truncated": labels_truncated,
            "tailscale_enabled": c.get("tailscaleEnabled"),
            "tailscale": (
                {
                    "online": ts.get("online"),
                    "version": ts.get("version"),
                    "update_available": ts.get("updateAvailable"),
                    "hostname": ts.get("hostname"),
                    "dns_name": ts.get("dnsName"),
                }
                if ts
                else None
            ),
        }
    )
    return out


def shape_container_sizes(data: dict | None, container_id: str | None) -> dict[str, Any]:
    """Pick one container's sizes out of the ``containers { id size* }`` list.
    All three are null-sized when the container is absent from the list."""
    docker = (data or {}).get("docker") or {}
    row = next((c for c in (docker.get("containers") or []) if c.get("id") == container_id), {})
    return {
        "size_root_fs": _size_from_bytes(row.get("sizeRootFs")),
        "size_rw": _size_from_bytes(row.get("sizeRw")),
        "size_log": _size_from_bytes(row.get("sizeLog")),
    }


def shape_containers(data: dict | None) -> list[Container | None]:
    docker = (data or {}).get("docker") or {}
    return [shape_container(c) for c in (docker.get("containers") or [])]


def shape_port_conflicts(data: dict | None) -> dict[str, Any]:
    pc = ((data or {}).get("docker") or {}).get("portConflicts") or {}

    def _containers(item: dict) -> list[dict[str, Any]]:
        return [{"id": x.get("id"), "name": x.get("name")} for x in (item.get("containers") or [])]

    container_ports = [
        {"private_port": i.get("privatePort"), "type": i.get("type"), "containers": _containers(i)}
        for i in (pc.get("containerPorts") or [])
    ]
    lan_ports = [
        {
            "lan_ip_port": i.get("lanIpPort"),
            "public_port": i.get("publicPort"),
            "type": i.get("type"),
            "containers": _containers(i),
        }
        for i in (pc.get("lanPorts") or [])
    ]
    return {
        "container_ports": container_ports,
        "lan_ports": lan_ports,
        "has_conflicts": bool(container_ports or lan_ports),
    }


def shape_docker_networks(data: dict | None) -> list[dict[str, Any]]:
    docker = (data or {}).get("docker") or {}
    return docker.get("networks") or []


# Full ANSI CSI escape: ``ESC [`` + params (0x30-0x3f) + intermediates (0x20-0x2f)
# + a final byte (0x40-0x7e). The `dockerContainerStats` subscription forwards raw
# ``docker stats`` terminal output, so the first event of each cycle carries a
# screen-repaint code (``ESC [ H`` / ``ESC [ J``) embedded in ``id`` (verified live
# in #27). Stripping only the ESC byte would leave ``[H`` garbage, so remove the
# whole sequence first, then any remaining bare C0/DEL control chars.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_C0_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_control(value: Any) -> Any:
    """Strip ANSI CSI escapes and C0/DEL control characters from a string.

    Non-strings pass through unchanged. Idempotent and pure. Applied to the
    subscription ``id`` **before** keying/dedup (so a polluted first-of-cycle id
    matches its clean ``list_docker_containers`` id) and defensively to the
    string metric fields.
    """
    if not isinstance(value, str):
        return value
    return _C0_CONTROL.sub("", _ANSI_CSI.sub("", value))


def shape_container_stats(events: list[dict] | None) -> list[dict[str, Any]]:
    """Shape collected ``dockerContainerStats`` ``next`` payloads into a snapshot.

    ``events`` is a list of GraphQL ``data`` objects (each ``{"dockerContainerStats":
    {...}}``), one per container, already deduped by the sampler. ``id`` and the
    pre-formatted string metrics are control-char-sanitized here (defence in depth on
    top of the sampler's keying). ``mem_usage``/``net_io``/``block_io`` are passed
    through as the API's pre-formatted strings (e.g. "65.56MiB / 31.25GiB") — they are
    composite usage/limit pairs, NOT single byte counts, so they deliberately do NOT
    use the ``{bytes, human}`` shape (no ``human`` without a matching ``bytes``).
    """
    out: list[dict[str, Any]] = []
    for event in events or []:
        stats = (event or {}).get("dockerContainerStats") or {}
        out.append(
            {
                "id": sanitize_control(stats.get("id")),
                "cpu_percent": stats.get("cpuPercent"),
                "mem_percent": stats.get("memPercent"),
                "mem_usage": sanitize_control(stats.get("memUsage")),
                "net_io": sanitize_control(stats.get("netIO")),
                "block_io": sanitize_control(stats.get("blockIO")),
            }
        )
    return out


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


HARDWARE_KINDS = ("gpu", "pci", "usb", "network")


def shape_hardware_inventory(data: dict | None, kind: str | None = None) -> dict[str, Any]:
    """Pick ``info.devices`` lists; a null/missing list becomes ``[]``.

    ``kind`` restricts the result to one device type. Device dicts pass through
    (the query already selects a curated field subset), except PCI
    ``blacklisted``, which upstream types as a String and is coerced to bool
    to match ``InfoGpu.blacklisted``.
    """
    devices = ((data or {}).get("info") or {}).get("devices") or {}
    kinds = (kind,) if kind else HARDWARE_KINDS
    out = {k: list(devices.get(k) or []) for k in kinds}
    if "pci" in out:
        out["pci"] = [
            {**d, "blacklisted": str(d["blacklisted"]).lower() == "true"}
            if d.get("blacklisted") is not None
            else d
            for d in out["pci"]
        ]
    return out


def shape_flash(data: dict | None) -> dict[str, Any]:
    flash = (data or {}).get("flash") or {}
    return {
        "guid": flash.get("guid"),
        "vendor": flash.get("vendor"),
        "product": flash.get("product"),
    }


_NOTIFICATION_DESC_MAX_CHARS = 500


def _truncate_description(item: dict[str, Any]) -> dict[str, Any]:
    desc = item.get("description")
    if isinstance(desc, str) and len(desc) > _NOTIFICATION_DESC_MAX_CHARS:
        return {
            **item,
            "description": desc[:_NOTIFICATION_DESC_MAX_CHARS] + _TRUNCATION_MARKER,
        }
    return item


def shape_notifications(data: dict | None) -> list[dict[str, Any]]:
    notifications = (data or {}).get("notifications") or {}
    return [_truncate_description(n) for n in (notifications.get("list") or [])]


def shape_warnings_and_alerts(data: dict | None) -> list[dict[str, Any]]:
    notifications = (data or {}).get("notifications") or {}
    return [_truncate_description(n) for n in (notifications.get("warningsAndAlerts") or [])]


def shape_notifications_overview(data: dict | None) -> dict[str, Any]:
    notifications = (data or {}).get("notifications") or {}
    return notifications.get("overview") or {}


def shape_ups(data: dict | None) -> list[dict[str, Any]]:
    return (data or {}).get("upsDevices") or []


def shape_network_interfaces(data: dict | None) -> list[dict[str, Any]]:
    return (data or {}).get("networkInterfaces") or []


def shape_me(data: dict | None) -> dict[str, Any]:
    return (data or {}).get("me") or {}


def shape_plugins(data: dict | None) -> list[dict[str, Any]]:
    """Shape the ``plugins`` root query: rich per-plugin metadata."""
    plugins = (data or {}).get("plugins") or []
    return [
        {
            "name": p.get("name"),
            "version": p.get("version"),
            "has_api_module": p.get("hasApiModule"),
            "has_cli_module": p.get("hasCliModule"),
            "source": "plugins",
        }
        for p in plugins
    ]


def shape_installed_unraid_plugins(
    data: dict | None, known_names: set[str] | None = None
) -> list[dict[str, Any]]:
    """Shape the ``installedUnraidPlugins`` root query: a coarser list of
    installed ``.plg`` filenames (OS-level plugins), with only the metadata
    this query provides (a name — no version/module info).

    ``known_names`` (the names already returned by :func:`shape_plugins`) lets
    callers dedupe entries that are already represented by the richer
    ``plugins`` list, so a plugin isn't shown twice under two different
    ``source`` values.
    """
    known = known_names or set()
    out = []
    for filename in (data or {}).get("installedUnraidPlugins") or []:
        base = filename[:-4] if filename.endswith(".plg") else filename
        if filename in known or base in known:
            continue
        out.append(
            {
                "name": filename,
                "version": None,
                "has_api_module": None,
                "has_cli_module": None,
                "source": "installed_unraid_plugins",
            }
        )
    return out


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


def _mutation_payload(
    data: dict | None, result_path: tuple[str, ...], *, allow_empty: bool = False
) -> Any:
    """Require the operation's result, without echoing upstream response values."""
    payload: Any = data
    for field in result_path:
        if not isinstance(payload, dict) or field not in payload or payload[field] is None:
            raise UnraidServerError(
                f"Mutation response is missing a non-null result for {'.'.join(result_path)}. "
                "Check the Unraid server logs and current state before retrying."
            )
        payload = payload[field]
    if not allow_empty and payload == {}:
        raise UnraidServerError(
            "Mutation returned an empty result. "
            "Check the Unraid server logs and current state before retrying."
        )
    return payload


def shape_mutation_result(data: dict | None, result_path: tuple[str, ...]) -> dict[str, Any]:
    """Extract a required mutation result and normalise Boolean/object payloads."""
    payload = _mutation_payload(data, result_path)
    if isinstance(payload, bool):
        return {"ok": payload}
    if isinstance(payload, dict):
        return _normalize_capacity(payload)
    raise UnraidServerError(
        "Mutation returned an invalid result: expected an object or Boolean. "
        "Check the Unraid server logs and current state before retrying."
    )


def shape_mutation_json_result(data: dict | None, result_path: tuple[str, ...]) -> dict[str, Any]:
    """Shape a ``JSON!`` scalar result (e.g. ``parityCheck.*``).

    The scalar's content is opaque (upstream returns the parity history list, which
    may be ``[]``), so any non-null value, including ``[]`` and ``{}``, means the
    action succeeded. A null/missing field is still an error.
    """
    payload = _mutation_payload(data, result_path, allow_empty=True)
    if isinstance(payload, bool):
        return {"ok": payload}
    out: dict[str, Any] = {"ok": True}
    if isinstance(payload, list):
        out["history_count"] = len(payload)
    return out


def shape_mutation_result_list(
    data: dict | None, result_path: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Extract a required list result; an explicit empty list is valid."""
    payload = _mutation_payload(data, result_path)
    if not isinstance(payload, list):
        raise UnraidServerError(
            "Mutation returned an invalid result: expected a list. "
            "Check the Unraid server logs and current state before retrying."
        )
    return list(payload)


_TOP_ALERTS_MAX = 5


_TEMP_UNIT_SYMBOLS = {"CELSIUS": "°C", "FAHRENHEIT": "°F", "KELVIN": "K", "RANKINE": "°R"}
_TEMP_REASONS_MAX = 5


def shape_health_temperature(data: dict | None) -> list[dict[str, Any]]:
    """Shape ``metrics.temperature.sensors`` down to real temperature sensors.

    lm_sensors chips surface fans (RPM), voltages and energy counters as
    ``CUSTOM`` sensors reported in CELSIUS with upstream ``CRITICAL`` status
    (e.g. a 2504 RPM fan at "2504 CELSIUS"). Every typed sensor (CPU/DISK/NVME/
    MOTHERBOARD/...) is kept; ``CUSTOM`` ones only when the name says "temp"
    (``CPU Temp``, ``MB Temp``, ``temp1``). Level prefers upstream
    ``current.status`` (see ``_shape_sensor``).
    """
    temperature = ((data or {}).get("metrics") or {}).get("temperature") or {}
    return [
        _shape_sensor(s)
        for s in temperature.get("sensors") or []
        if s and (s.get("type") != "CUSTOM" or "temp" in (s.get("name") or "").lower())
    ]


def _temperature_signals(
    sensors: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], bool, bool]:
    """(summary, reasons, any_critical, any_warning) for shaped sensors."""

    def reading(s: dict[str, Any]) -> str:
        current = s.get("current") or {}
        unit = _TEMP_UNIT_SYMBOLS.get(current.get("unit"), current.get("unit") or "")
        return f"{s.get('name') or 'unnamed'} {current.get('value')}{unit}"

    def hottest_first(level: str) -> list[dict[str, Any]]:
        found = [s for s in sensors if s.get("level") == level]
        return sorted(
            found,
            key=lambda s: (s["current"].get("value") is None, -(s["current"].get("value") or 0)),
        )

    critical, warning = hottest_first("critical"), hottest_first("warning")
    reasons: list[str] = []
    for label, found in (("critical", critical), ("warning", warning)):
        reasons.extend(f"Temperature {label}: {reading(s)}" for s in found[:_TEMP_REASONS_MAX])
        if len(found) > _TEMP_REASONS_MAX:
            reasons.append(f"Temperature {label}: {len(found) - _TEMP_REASONS_MAX} more sensors")
    readable = [s for s in sensors if (s.get("current") or {}).get("value") is not None]
    hot = max(readable, key=lambda s: s["current"]["value"], default=None)
    summary = {
        "hottest": {
            "name": hot.get("name"),
            "value": hot["current"]["value"],
            "unit": hot["current"].get("unit"),
            "level": hot.get("level"),
        }
        if hot
        else None,
        "warning_count": len(warning),
        "critical_count": len(critical),
    }
    return summary, reasons, bool(critical), bool(warning)


def summarize_health(
    array_out: dict[str, Any],
    ups_list: list[dict[str, Any]],
    notifications_overview: dict[str, Any],
    checks: dict[str, str] | None = None,
    top_alerts: list[dict[str, Any]] | None = None,
    temperature_sensors: list[dict[str, Any]] | None = None,
) -> HealthSummary:
    """Compose a compact, triage-friendly health roll-up from the shaped parts.

    ``top_alerts`` is the shaped ``warningsAndAlerts`` list, or None when the API
    build lacks that query (the key is then omitted from the result).
    ``temperature_sensors`` is ``shape_health_temperature`` output, or None when
    the temperature check failed (the ``temperature`` key is then omitted).
    """
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
    unhealthy = [
        d
        for d in disks
        if d.get("health") not in ("healthy", None)
        or (d.get("color") or "").lower().startswith(("red", "yellow"))
    ]
    unread = (notifications_overview or {}).get("unread") or {}
    checks = (
        checks if checks is not None else dict.fromkeys(("array", "ups", "notifications"), "ok")
    )
    reasons = []
    critical = False
    for disk in unhealthy:
        red = (disk.get("color") or "").lower().startswith("red")
        critical |= red or disk.get("health") in ("red", "failed", "critical", "missing")
        yellow = (disk.get("color") or "").lower().startswith("yellow")
        health = "red" if red else "yellow" if yellow else disk.get("health")
        reasons.append(f"Disk {disk.get('name') or 'unnamed'} is {health}")
    for severity in ("alert", "warning"):
        if unread.get(severity):
            reasons.append(f"Unread {severity} notifications: {unread[severity]}")
    parity = array_out.get("parity_check") or {}
    if (parity.get("errors") or 0) > 0:
        reasons.append(f"Parity check reported {parity['errors']} errors")
    for ups in ups_list or []:
        status = (ups.get("status") or "").upper().split()
        on_battery = "ONBATT" in status or status == ["ON", "BATTERY"]
        low_battery = "LOWBATT" in status or status == ["LOW", "BATTERY"]
        if not on_battery and not low_battery:
            continue
        name = ups.get("name") or "unnamed"
        if low_battery:
            critical = True
            reasons.append(f"UPS {name} reports low battery")
        if not on_battery:
            continue
        reasons.append(f"UPS {name} is on battery")
        battery = ups.get("battery") or {}
        charge = battery.get("chargeLevel")
        runtime = battery.get("estimatedRuntime")
        if charge is not None and charge < 20:
            critical = True
            reasons.append(f"UPS {name} battery charge is {charge}% (<20%)")
        if runtime is not None and runtime < 300:
            critical = True
            reasons.append(f"UPS {name} runtime is {runtime} seconds (<5 minutes)")
    temperature = None
    if temperature_sensors is not None:
        temperature, temp_reasons, temp_critical, _ = _temperature_signals(temperature_sensors)
        critical |= temp_critical
        reasons.extend(temp_reasons)
    if top_alerts and not (unread.get("alert") or unread.get("warning")):
        # The overview query may have failed while warningsAndAlerts succeeded.
        reasons.append(f"Unread warning/alert notifications: {len(top_alerts)}")
    attention = bool(reasons)
    failed_checks = [name for name, status in checks.items() if status == "failed"]
    reasons.extend(f"{name.capitalize()} check failed or is unsupported" for name in failed_checks)
    overall = (
        "critical"
        if critical
        else "attention"
        if attention
        else "degraded"
        if failed_checks
        else "ok"
    )
    result = {
        "overall": overall,
        "reasons": reasons,
        "checks": checks,
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
    if temperature is not None:
        result["temperature"] = temperature
    if top_alerts is not None:
        result["top_alerts"] = [
            {"title": a.get("title"), "importance": a.get("importance")}
            for a in top_alerts[:_TOP_ALERTS_MAX]
        ]
    return result
