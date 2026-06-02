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

SYSTEM_INFO = """
query GetSystemInfo {
  info {
    os { platform distro release kernel arch hostname uptime }
    cpu { manufacturer brand cores threads processors socket }
    memory { layout { type clockSpeed manufacturer } }
    baseboard { manufacturer model version }
    system { manufacturer model version serial }
    versions { core { unraid api kernel } }
    machineId
    time
  }
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

LIST_VMS = """
query ListVMs {
  vms { id domains { id name state } }
}
"""

SHARES = """
query GetSharesInfo {
  shares { id name free used size comment allocator cache cow color }
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

VM_START = "mutation StartVM($id: PrefixedID!) { vm { start(id: $id) } }"
VM_STOP = "mutation StopVM($id: PrefixedID!) { vm { stop(id: $id) } }"
VM_PAUSE = "mutation PauseVM($id: PrefixedID!) { vm { pause(id: $id) } }"
VM_RESUME = "mutation ResumeVM($id: PrefixedID!) { vm { resume(id: $id) } }"
VM_FORCE_STOP = "mutation ForceStopVM($id: PrefixedID!) { vm { forceStop(id: $id) } }"
VM_REBOOT = "mutation RebootVM($id: PrefixedID!) { vm { reboot(id: $id) } }"

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
