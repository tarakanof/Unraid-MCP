"""GraphQL operation strings for the Unraid 7.x API.

Field names, types, enums, and the ``PrefixedID`` scalar behaviour were
validated against the generated SDL in ``unraid/api`` (``generated-schema.graphql``)
and the working ``jmagar/unraid-mcp`` reference implementation.

Unit notes baked into downstream formatting:
  * ``ArrayDisk`` sizes (size/fsSize/fsFree/fsUsed) and ``Share`` free/used/size
    are in **KiB**.
  * ``Disk.size`` (physical disks) is in **bytes**.
"""

from __future__ import annotations

# Shared ArrayDisk selection (used for array data/parity/cache/boot members).
_ARRAY_DISK_FIELDS = (
    "id idx name device size status rotational temp numReads numWrites numErrors "
    "fsSize fsFree fsUsed type fsType color warning critical comment"
)

# ── Queries ────────────────────────────────────────────────────────────────

# Cheap startup probe: resolves the connected API/Unraid versions so tools can
# self-diagnose and degrade gracefully. Selects only fields present on every
# supported build. UPPER_CASE so the schema-drift script validates it too.
API_PROBE = """
query GetApiProbe {
  info {
    versions { core { api unraid } }
  }
}
"""

SYSTEM_INFO = """
query GetSystemInfo {
  info {
    os { platform distro release kernel arch hostname uptime }
    cpu { manufacturer brand cores threads processors socket }
    memory { layout { type clockSpeed manufacturer } }
    baseboard { manufacturer model version }
    system { manufacturer model version }
    versions { core { unraid api kernel } }
    time
  }
}
"""

# Live utilization snapshot. Selects conservatively — omits `hottest`/`coolest`
# on TemperatureSummary (they force full nested TemperatureSensor selections)
# and `percentUser`/`percentSystem`/etc. on CpuLoad (per-core total is enough
# signal for an agent). Every field verified against the upstream SDL.
SYSTEM_METRICS = """
query GetSystemMetrics {
  metrics {
    cpu { percentTotal cpus { percentTotal } }
    memory { total used free available percentTotal swapTotal swapUsed swapFree percentSwapTotal }
    temperature {
      summary { average warningCount criticalCount }
      sensors { name current { value unit } }
    }
  }
}
"""

SERVICES = """
query GetServices {
  services { id name online uptime { timestamp } version }
}
"""

_ARRAY_DISK_SELECTION = "{ " + _ARRAY_DISK_FIELDS + " }"

ARRAY_STATUS = """
query GetArrayStatus {
  array {
    id
    state
    capacity { kilobytes { free used total } disks { free used total } }
    boot __DISK__
    parities __DISK__
    disks __DISK__
    caches __DISK__
    parityCheckStatus { progress speed errors status paused running correcting }
  }
}
""".replace("__DISK__", _ARRAY_DISK_SELECTION)

LIST_DISKS = """
query ListPhysicalDisks {
  disks {
    id device name vendor type size interfaceType
    smartStatus temperature isSpinning serialNum
  }
}
"""

DISK_DETAILS = """
query GetDiskDetails($id: PrefixedID!) {
  disk(id: $id) {
    id device name vendor type serialNum firmwareRevision
    size interfaceType smartStatus temperature isSpinning
    partitions { name fsType size }
  }
}
"""

PARITY_STATUS = """
query GetParityStatus {
  array { parityCheckStatus { progress speed errors status paused running correcting } }
}
"""

PARITY_HISTORY = """
query GetParityHistory {
  parityHistory { date duration speed status errors progress correcting paused running }
}
"""

LIST_CONTAINERS = """
query ListDockerContainers {
  docker {
    containers {
      id names image state status autoStart
      ports { ip privatePort publicPort type }
    }
  }
}
"""

DOCKER_NETWORKS = """
query GetDockerNetworks {
  docker { networks { id name driver scope internal attachable } }
}
"""

CONTAINER_LOGS = """
query GetContainerLogs($id: PrefixedID!, $since: DateTime, $tail: Int) {
  docker { logs(id: $id, since: $since, tail: $tail) { containerId lines { timestamp message } cursor } }
}
"""

DOCKER_UPDATE_STATUSES = """
query GetDockerUpdateStatuses {
  docker { containerUpdateStatuses { name updateStatus } }
}
"""

DOCKER_CONTAINER = """
query GetDockerContainer($id: PrefixedID!) {
  docker {
    container(id: $id) {
      id names image state status autoStart
      ports { ip privatePort publicPort type }
    }
  }
}
"""

# ── Subscriptions ────────────────────────────────────────────────────────────

# graphql-transport-ws subscription sampled one-shot by ``get_docker_container_stats``.
# Takes NO arguments and emits ONE container per event (``DockerContainerStats!``,
# singular); the sampler accumulates events keyed by ``id`` until a full cycle is
# seen (see tools/docker.py). ``memUsage``/``netIO``/``blockIO`` are pre-formatted
# strings from the API (e.g. "65.56MiB / 31.25GiB"), NOT byte counts.
DOCKER_CONTAINER_STATS = """
subscription DockerContainerStats {
  dockerContainerStats { id cpuPercent memUsage memPercent netIO blockIO }
}
"""

LIST_VMS = """
query ListVMs {
  vms { id domains { id name state } }
}
"""

# Fallback for older Unraid API builds whose `vms` type does not yet expose
# `domains` (only the legacy `domain` field). See ``fetch_vms`` in
# ``tools/vm.py`` for the retry logic that selects between these two.
LIST_VMS_LEGACY = """
query ListVMsLegacy {
  vms { id domain { id name state } }
}
"""

SHARES = """
query GetSharesInfo {
  shares {
    id name free used size comment allocator cache cow color
    include exclude splitLevel floor luksStatus
  }
}
"""

# Root ``systemTime`` — separate from ``info`` (see SYSTEM_INFO above).
SYSTEM_TIME = """
query GetSystemTime {
  systemTime { currentTime timeZone useNtp ntpServers }
}
"""

# Root ``flash`` — a separate root query, NOT nested under ``info``. Fetched as
# a second, independently-degrading call in ``fetch_system_info`` so older API
# builds without this field still return system info (see tools/system.py).
FLASH = """
query GetFlashInfo {
  flash { guid vendor product }
}
"""

NOTIFICATIONS_OVERVIEW = """
query GetNotificationsOverview {
  notifications {
    overview {
      unread { info warning alert total }
      archive { info warning alert total }
    }
  }
}
"""

LIST_NOTIFICATIONS = """
query ListNotifications($filter: NotificationFilter!) {
  notifications {
    list(filter: $filter) {
      id title subject description importance link type timestamp formattedTimestamp
    }
  }
}
"""

UPS_DEVICES = """
query GetUpsDevices {
  upsDevices {
    id name model status
    battery { chargeLevel estimatedRuntime health }
    power { loadPercentage inputVoltage outputVoltage }
  }
}
"""

CONNECT_STATUS = """
query GetConnectStatus {
  registration { id type state expiration updateExpiration }
  remoteAccess { accessType forwardType port }
}
"""

NETWORK_INTERFACES = """
query GetNetworkInterfaces {
  networkInterfaces {
    id name description macAddress mtu speed duplex operstate type virtual internal
    ipv4Addresses { address }
    ipv6Addresses { address }
  }
}
"""

ME = """
query GetMe {
  me { id name description roles }
}
"""

# Rich per-plugin metadata (name/version/module flags).
PLUGINS = """
query ListPlugins {
  plugins { name version hasApiModule hasCliModule }
}
"""

# Root ``installedUnraidPlugins`` — a separate root query returning just the
# installed .plg filenames (a coarser, OS-level view than ``plugins`` above).
# Fetched as a second, independently-degrading call in ``fetch_plugins`` (see
# tools/misc.py), matching the ``SYSTEM_INFO``/``FLASH`` pattern.
INSTALLED_UNRAID_PLUGINS = """
query GetInstalledUnraidPlugins {
  installedUnraidPlugins
}
"""

# ── Mutations ────────────────────────────────────────────────────────────────

START_ARRAY = """
mutation StartArray {
  array {
    setState(input: { desiredState: START }) {
      state
      capacity { kilobytes { free used total } }
    }
  }
}
"""

STOP_ARRAY = """
mutation StopArray {
  array { setState(input: { desiredState: STOP }) { state } }
}
"""

START_PARITY = """
mutation StartParityCheck($correct: Boolean!) {
  parityCheck { start(correct: $correct) }
}
"""

PAUSE_PARITY = "mutation PauseParityCheck { parityCheck { pause } }"
RESUME_PARITY = "mutation ResumeParityCheck { parityCheck { resume } }"
CANCEL_PARITY = "mutation CancelParityCheck { parityCheck { cancel } }"

START_CONTAINER = """
mutation StartContainer($id: PrefixedID!) {
  docker { start(id: $id) { id names state status } }
}
"""

STOP_CONTAINER = """
mutation StopContainer($id: PrefixedID!) {
  docker { stop(id: $id) { id names state status } }
}
"""

RESTART_CONTAINER = """
mutation RestartContainer($id: PrefixedID!) {
  docker { restart(id: $id) { id names state status } }
}
"""

PAUSE_CONTAINER = """
mutation PauseContainer($id: PrefixedID!) {
  docker { pause(id: $id) { id names state status } }
}
"""

UNPAUSE_CONTAINER = """
mutation UnpauseContainer($id: PrefixedID!) {
  docker { unpause(id: $id) { id names state status } }
}
"""

# Pull the latest image and recreate the container. Returns the recreated
# DockerContainer; `updateContainers` recreates a batch and returns a LIST.
UPDATE_CONTAINER = """
mutation UpdateContainer($id: PrefixedID!) {
  docker { updateContainer(id: $id) { id names state status } }
}
"""

UPDATE_CONTAINERS = """
mutation UpdateContainers($ids: [PrefixedID!]!) {
  docker { updateContainers(ids: $ids) { id names state status } }
}
"""

VM_START = "mutation StartVM($id: PrefixedID!) { vm { start(id: $id) } }"
VM_STOP = "mutation StopVM($id: PrefixedID!) { vm { stop(id: $id) } }"
VM_PAUSE = "mutation PauseVM($id: PrefixedID!) { vm { pause(id: $id) } }"
VM_RESUME = "mutation ResumeVM($id: PrefixedID!) { vm { resume(id: $id) } }"
VM_FORCE_STOP = "mutation ForceStopVM($id: PrefixedID!) { vm { forceStop(id: $id) } }"
VM_REBOOT = "mutation RebootVM($id: PrefixedID!) { vm { reboot(id: $id) } }"
VM_RESET = "mutation ResetVM($id: PrefixedID!) { vm { reset(id: $id) } }"

LOG_FILES = """
query GetLogFiles {
  logFiles { name path size modifiedAt }
}
"""

LOG_FILE = """
query GetLogFile($path: String!, $lines: Int, $startLine: Int) {
  logFile(path: $path, lines: $lines, startLine: $startLine) {
    path content totalLines startLine
  }
}
"""

ARCHIVE_NOTIFICATION = """
mutation ArchiveNotification($id: PrefixedID!) {
  archiveNotification(id: $id) { id title importance type }
}
"""

ARCHIVE_ALL_NOTIFICATIONS = """
mutation ArchiveAllNotifications($importance: NotificationImportance) {
  archiveAll(importance: $importance) {
    unread { info warning alert total }
    archive { info warning alert total }
  }
}
"""

UNREAD_NOTIFICATION = """
mutation UnreadNotification($id: PrefixedID!) {
  unreadNotification(id: $id) { id title importance type }
}
"""

DELETE_NOTIFICATION = """
mutation DeleteNotification($id: PrefixedID!, $type: NotificationType!) {
  deleteNotification(id: $id, type: $type) {
    unread { info warning alert total }
    archive { info warning alert total }
  }
}
"""

ARCHIVE_NOTIFICATIONS = """
mutation ArchiveNotifications($ids: [PrefixedID!]!) {
  archiveNotifications(ids: $ids) {
    unread { info warning alert total }
    archive { info warning alert total }
  }
}
"""

UNARCHIVE_NOTIFICATIONS = """
mutation UnarchiveNotifications($ids: [PrefixedID!]!) {
  unarchiveNotifications(ids: $ids) {
    unread { info warning alert total }
    archive { info warning alert total }
  }
}
"""

UNARCHIVE_ALL_NOTIFICATIONS = """
mutation UnarchiveAllNotifications($importance: NotificationImportance) {
  unarchiveAll(importance: $importance) {
    unread { info warning alert total }
    archive { info warning alert total }
  }
}
"""

DELETE_ARCHIVED_NOTIFICATIONS = """
mutation DeleteArchivedNotifications {
  deleteArchivedNotifications {
    unread { info warning alert total }
    archive { info warning alert total }
  }
}
"""

CREATE_NOTIFICATION = """
mutation CreateNotification($input: NotificationData!) {
  createNotification(input: $input) {
    id title subject description importance link type timestamp
  }
}
"""

# ── Dangerous-tier mutations ─────────────────────────────────────────────────
# High-blast-radius array-topology and container-removal ops. Registered only
# when UNRAID_MCP_ALLOW_MUTATIONS *and* UNRAID_MCP_ALLOW_DANGEROUS are both set.
# UnraidArray/ArrayDisk returns select a minimal, existing-shaped selection.

MOUNT_ARRAY_DISK = """
mutation MountArrayDisk($id: PrefixedID!) {
  array { mountArrayDisk(id: $id) { id name status } }
}
"""

UNMOUNT_ARRAY_DISK = """
mutation UnmountArrayDisk($id: PrefixedID!) {
  array { unmountArrayDisk(id: $id) { id name status } }
}
"""

CLEAR_ARRAY_DISK_STATISTICS = """
mutation ClearArrayDiskStatistics($id: PrefixedID!) {
  array { clearArrayDiskStatistics(id: $id) }
}
"""

ADD_DISK_TO_ARRAY = """
mutation AddDiskToArray($input: ArrayDiskInput!) {
  array { addDiskToArray(input: $input) { id state } }
}
"""

REMOVE_DISK_FROM_ARRAY = """
mutation RemoveDiskFromArray($input: ArrayDiskInput!) {
  array { removeDiskFromArray(input: $input) { id state } }
}
"""

REMOVE_DOCKER_CONTAINER = """
mutation RemoveDockerContainer($id: PrefixedID!, $withImage: Boolean) {
  docker { removeContainer(id: $id, withImage: $withImage) }
}
"""

# Fleet-wide: pull + recreate EVERY container with an available update. Returns
# the LIST of recreated DockerContainers.
UPDATE_ALL_CONTAINERS = """
mutation UpdateAllContainers {
  docker { updateAllContainers { id names state status } }
}
"""
