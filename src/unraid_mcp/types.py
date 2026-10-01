"""Plain output contracts for the core read tools, independent of MCP."""

from typing import Annotated, Any, Literal, NotRequired

from pydantic import ConfigDict, Field
from typing_extensions import TypedDict


class Size(TypedDict):
    bytes: Annotated[int | None, Field(description="Size in bytes; null if unavailable.")]
    human: Annotated[str | None, Field(description="Human-readable binary size, such as 1.0 TiB.")]


class ContainerPort(TypedDict):
    private: Annotated[int | None, Field(description="Port inside the container.")]
    public: Annotated[int | None, Field(description="Published host port, if mapped.")]
    type: str | None
    ip: str | None


class TailscaleStatus(TypedDict):
    online: bool | None
    version: str | None
    update_available: bool | None
    hostname: str | None
    dns_name: str | None


class Container(TypedDict):
    """List item and detail share the same shaped fields."""

    id: str | None
    name: Annotated[
        str | None, Field(description="First container name without the leading slash.")
    ]
    names: list[str]
    image: str | None
    state: str | None
    status: str | None
    auto_start: bool | None
    auto_start_order: int | None
    update_available: bool | None
    orphaned: bool | None
    web_ui_url: str | None
    network_mode: str | None
    ports: list[ContainerPort]
    # Detail-only fields (get_docker_container); absent from the list view.
    rebuild_ready: NotRequired[bool | None]
    lan_ip_ports: NotRequired[list[Any]]
    icon_url: NotRequired[str | None]
    project_url: NotRequired[str | None]
    support_url: NotRequired[str | None]
    template_path: NotRequired[str | None]
    auto_start_wait: NotRequired[int | None]
    mounts: NotRequired[list[Any]]
    labels: NotRequired[Any]
    labels_truncated: NotRequired[bool]
    tailscale_enabled: NotRequired[bool | None]
    tailscale: NotRequired[TailscaleStatus | None]
    # Only with include_sizes=true.
    size_root_fs: NotRequired[Size]
    size_rw: NotRequired[Size]
    size_log: NotRequired[Size]


class DiskPartition(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    name: str | None
    fsType: str | None
    size: Size


class Disk(TypedDict):
    """Physical disk list item and detail; unselected fields remain null."""

    id: str | None
    name: str | None
    device: str | None
    vendor: str | None
    type: str | None
    serial: str | None
    interface: str | None
    smart_status: str | None
    temp_c: Annotated[int | float | None, Field(description="Disk temperature in degrees Celsius.")]
    spinning: bool | None
    size: Size
    firmware: str | None
    partitions: list[DiskPartition]


class ArrayDisk(TypedDict):
    name: str | None
    device: str | None
    type: str | None
    status: str | None
    health: str
    temp_c: int | None
    fs_type: str | None
    size: Size
    fs_used: Size
    fs_free: Size
    reads: int | str | None
    writes: int | str | None
    errors: int | str | None
    color: str | None
    spinning: bool | None
    format: str | None
    transport: str | None
    exportable: bool | None


class Capacity(TypedDict):
    total: Size
    used: Size
    free: Size


class UnhealthyDisk(TypedDict):
    name: str | None
    health: str | None
    status: str | None


class ParityCheck(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    progress: NotRequired[int | None]
    speed: NotRequired[str | None]
    errors: NotRequired[int | None]
    status: NotRequired[str | None]
    paused: NotRequired[bool | None]
    running: NotRequired[bool | None]
    correcting: NotRequired[bool | None]


class HealthUPS(TypedDict):
    name: str | None
    status: str | None
    battery_pct: Annotated[int | None, Field(description="UPS battery charge percentage.")]


class NotificationCounts(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    info: NotRequired[int | None]
    warning: NotRequired[int | None]
    alert: NotRequired[int | None]
    total: NotRequired[int | None]


class TopAlert(TypedDict):
    title: str | None
    importance: str | None


class HealthSummary(TypedDict):
    overall: Annotated[
        Literal["ok", "attention", "critical", "degraded"],
        Field(
            description="Critical: failed disks or UPS low/depleting battery. Attention: other "
            "unhealthy disks, unread alerts/warnings, UPS on battery, parity errors. Degraded: "
            "only failed sub-queries."
        ),
    ]
    reasons: Annotated[list[str], Field(description="One entry per signal behind overall.")]
    checks: Annotated[
        dict[str, Literal["ok", "failed", "not_configured"]],
        Field(description="Status of the array, ups and notifications queries."),
    ]
    array_state: str | None
    capacity: Capacity | None
    disk_count: Annotated[int, Field(description="Assigned disks, excluding empty array slots.")]
    unhealthy_disks: list[UnhealthyDisk]
    parity_check: ParityCheck | None
    ups: list[HealthUPS]
    notifications_unread: NotificationCounts
    top_alerts: NotRequired[list[TopAlert]]
