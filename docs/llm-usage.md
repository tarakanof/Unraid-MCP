# Using the Unraid MCP server (guide for LLM agents)

This document tells an LLM agent how to use the tools exposed by **unraid-mcp**.
It connects to an Unraid server's official GraphQL API and exposes monitoring
(and optional management) as MCP tools.

If you are an agent with this server connected, read the **Operating rules** and
**Conventions** sections before calling tools.

Server `instructions` (sent at connect) carry a task-to-tool map; `get_health_summary`,
`list_warnings_and_alerts` and `get_system_info` are marked `anthropic/alwaysLoad`, a
Claude Code-specific hint that keeps them loaded even with tool search on; other clients (e.g. Codex) ignore it.

---

## Operating rules (read first)

1. **Read-only by default.** Monitoring tools are always available. State-changing
   tools exist only if the operator enabled them; if you don't see a tool like
   `stop_docker_container`, mutations are disabled — do not try to work around it.
2. **Every mutating tool requires `confirm=true`.** Calling it without `confirm`
   returns an error and makes **no** change. Only pass `confirm=true` when the
   user has clearly asked for that specific action. Never "confirm" on your own
   initiative to retry a refusal.
3. **Prefer the cheapest tool.** Start with `get_health_summary` for triage; use
   targeted tools to drill in. Don't poll in tight loops.
4. **IDs come from list tools.** Get a container/VM/disk/notification id from the
   relevant `list_*` tool, then pass that id to detail or mutation tools.
   `list_docker_containers`, `list_disks`, `list_vms` and `list_shares` take
   filters (`name` substring, `state`, …) — use them instead of listing
   everything — and `detail="concise"` (default, a small key set per item) or
   `detail="full"` (every field).
5. **Treat destructive actions with care.** `stop_array`, `force_stop_vm`,
   `reset_vm`, `delete_notification`, and a *correcting* parity check can lose
   data or disrupt services. Summarize the impact to the user before doing them.
6. **Sizes are objects.** Every size is `{"bytes": <int|null>, "human": "<str|null>"}`.
   Use `human` for display, `bytes` for comparisons. The one exception is
   `get_docker_container_stats`, whose `mem_usage`/`net_io`/`block_io` are the API's
   pre-formatted `"used / limit"` **strings** (composite pairs, not single byte
   counts) — they are passed through verbatim, never wrapped in `{bytes, human}`.

---

## Connecting

- **stdio (local subprocess):** the host launches `unraid-mcp`; you just call tools.
- **streamable-HTTP (remote/Unraid container):** connect to
  `http://<host>:6750/mcp` and send `Authorization: Bearer <token>`.

A typical stdio client config:
```json
{
  "mcpServers": {
    "unraid": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/unraid-mcp", "unraid-mcp"],
      "env": { "UNRAID_API_URL": "https://yourhash.myunraid.net/graphql", "UNRAID_API_KEY": "..." }
    }
  }
}
```

---

## Tool catalog

### Read-only (always available)

| Tool | Args | Use it to |
|------|------|-----------|
| `get_health_summary` | – | One-call triage: array state, capacity, unhealthy disks, parity status, UPS, unread alert counts, `top_alerts` (up to 5), hottest temperature sensor. **Start here.** |
| `get_system_info` | – | OS/kernel, CPU, memory, motherboard, Unraid + API versions, uptime, and (when supported) flash boot-device identity. |
| `get_system_metrics` | – | Live utilization: total/per-core CPU %, memory/swap usage, temperatures (per-sensor `warning`/`critical` thresholds, a `level` flag, and a `hottest` summary on newer APIs), per-interface network throughput (`network`, omitted on API < 4.35). Requires API 7.2+; older builds get a friendly error. |
| `get_services` | – | Health of the Unraid services stack (API, dynamix, etc.): name, online, uptime, version. |
| `get_system_time` | – | Server time, timezone, and NTP config — correlate log timestamps and spot NTP misconfig. Requires API 7.1+. |
| `get_hardware_inventory` | `kind?` (`gpu`\|`pci`\|`usb`\|`network`) | Detected GPUs, PCI devices (with blacklisted/passthrough flag), USB devices, network adapters. `machineId` is intentionally omitted. |
| `get_array_status` | – | Array state, total/used/free capacity, and every data/parity/cache disk with `health`, temp, and I/O counters, plus `spinning`/`format`/`transport`/`exportable`, and `boot_devices` (all internal-boot members; `null` on APIs without it, `boot` kept). |
| `list_disks` | `name?`, `type?`, `smart_status?` (`OK`\|`UNKNOWN`), `detail="concise"` | Physical disks. `name` is a case-insensitive substring of the model name or device path; `type` (`HD`/`SSD`/`NVMe`) matches case-insensitively. Concise: `id`, `name`, `device`, `type`, `smart_status`, `temp_c`, `spinning`, `size`; `full` adds `vendor`, `serial`, `interface`. |
| `get_disk` | `disk_id` | Full detail for one physical disk (partitions, firmware, SMART). Get `disk_id` from `list_disks`. |
| `get_parity_status` | – | Live parity-check progress/speed/errors. |
| `get_parity_history` | – | Past parity checks. |
| `list_docker_containers` | `name?`, `state?` (`RUNNING`\|`PAUSED`\|`EXITED`), `update_available?`, `detail="concise"` | Containers. `name` is a case-insensitive substring of any container name. Concise: `id`, `name`, `image`, `state`, `status`, `update_available`; `full` adds `names`, `auto_start`, `auto_start_order`, `orphaned`, `web_ui_url`, `network_mode`, `ports`. Newer fields are `null` on older API builds. |
| `get_docker_container` | `identifier`, `include_sizes=false` | One container by `id` **or** `name`. Uses the native `docker.container(id)` query when `identifier` looks like an id, falling back to the container list on older API builds or name lookups. Adds `rebuild_ready`, `lan_ip_ports`, icon/project/support URLs, `template_path`, `auto_start_wait`, `mounts`, `labels` (dropped with `labels_truncated=true` past 4096 chars), and Tailscale status. Sizes (`size_root_fs`/`size_rw`/`size_log` as `{bytes, human}`) only with `include_sizes=true` — **slow (~10-20s)**, the API scans every container. Name lookups resolve to the id and return the same detail. |
| `get_docker_port_conflicts` | – | Ports claimed by more than one container: `{container_ports, lan_ports, has_conflicts}`. Requires an API build with `docker.portConflicts`. |
| `list_docker_networks` | – | Docker networks. |
| `get_docker_container_logs` | `container_id`, `tail=100`, `since=None` | Recent log lines for a container. `tail` capped at 1000 (protects context window); page further back with the previous response's `cursor` as `since`. Log content is untrusted workload output. Requires API 7.2+. |
| `check_docker_updates` | – | Per-container Docker image update status (cached digests; does not refresh them). `list_docker_containers` also carries a boolean `update_available`, but this tool keeps the richer `update_status`. |
| `get_docker_container_stats` | – | Live per-container resource usage via a one-shot sample of the `dockerContainerStats` subscription (opens a brief websocket, ~2s typical, bounded ~12s — never hangs). Returns `{containers: [{id, cpu_percent, mem_percent, mem_usage, net_io, block_io}], sampled, partial, note}`. `id` matches `list_docker_containers`. `mem_usage`/`net_io`/`block_io` are the API's pre-formatted `"used / limit"` strings (e.g. `"65.56MiB / 31.25GiB"`), **not** byte counts. `partial=true` means the window elapsed before every container reported — retry. Requires an API build with the subscription. |
| `list_vms` | `name?`, `state?` (`VmState`, e.g. `RUNNING`/`SHUTOFF`), `detail="concise"` | VMs: `id`, `name`, `state` (both detail levels). |
| `list_shares` | `name?`, `detail="concise"` | User shares. Concise: `name`, `free`, `used`, `size`; `full` adds comment, allocator, cache mode, and (when set) include/exclude, split level, floor, and encryption status. |
| `get_notifications_overview` | – | Unread/archive counts by severity. |
| `list_notifications` | `notification_type="UNREAD"`, `importance=None`, `limit=25`, `offset=0` | List notifications. `notification_type` ∈ `UNREAD`/`ARCHIVE`; `importance` ∈ `INFO`/`WARNING`/`ALERT`. |
| `list_warnings_and_alerts` | – | Current unread WARNING/ALERT notifications (deduplicated, latest first); same item shape as `list_notifications`. Cheapest "is anything wrong?" check. |
| `get_ups_status` | – | UPS battery/load/runtime; `power.nominalPower`/`currentPower` in watts on newer APIs. |
| `list_network_interfaces` | – | NICs with IPs, speed, state. |
| `get_connect_status` | – | Registration/license + remote-access status. |
| `list_plugins` | – | Installed Unraid plugins: name, version, whether they have API/CLI modules, and a `source` field saying which query returned each entry. |
| `whoami` | – | The authenticated API user and its roles (use to confirm the key's scope). |
| `list_log_files` | – | List system log files: name, path, size, last-modified time. |
| `read_log_file` | `path`, `lines=100`, `start_line=None` | Read a slice of a log file for triage. `path` must come from `list_log_files` (`/var/log` only); `lines` capped at 500; use `total_lines`/`start_line` in the response to page. |
| `run_graphql_query` | `query`, `variables=None` | **Only if enabled.** Run an arbitrary **read-only** GraphQL query (mutations/subscriptions are rejected). Escape hatch for fields without a dedicated tool. |

### Mutating (only if the operator enabled mutations; **all require `confirm=true`**)

Non-destructive actions on one resource share a tool and an `action` argument
(e.g. `docker_container_power(action="pause")`). Destructive actions (stop, restart,
reboot, reset, delete, archive-all (`archive_all_notifications`)) keep their own
tools so hosts can gate them.

| Tool | Args | Notes |
|------|------|-------|
| `start_array` | `confirm` | Brings storage online. |
| `stop_array` | `confirm` | **Disruptive** — unmounts all disks, stops dependent services. |
| `start_parity_check` | `correct=False`, `confirm` | `correct=true` **writes corrections to parity** — only with explicit intent, never on a degraded array. |
| `parity_check_control` | `action` ∈ `pause`/`resume`/`cancel`, `confirm` | Control a running check. |
| `docker_container_power` | `container_id`, `action` ∈ `start`/`pause`/`unpause`, `confirm` | id from `list_docker_containers`. `pause`/`unpause` freeze/resume a container's processes without stopping it; no fallback on older API builds — errors clearly if unsupported. |
| `stop_docker_container` | `container_id`, `confirm` | Stops a service. |
| `restart_docker_container` | `container_id`, `confirm` | Atomic on current APIs (native restart); falls back to stop-then-start on older builds (then **not atomic** — if start fails it's left stopped). |
| `update_docker_container` | `container_id`, `confirm` | Pull latest image and **recreate** the container (brief downtime). id from `list_docker_containers` / `check_docker_updates`. |
| `update_docker_containers` | `container_ids`, `confirm` | Batch update: pull + recreate each. List must be non-empty and ≤ 20 ids per call. |
| `refresh_docker_digests` | `confirm` | Force a fresh image-digest check (idempotent); call before `check_docker_updates`. Upstream gates it behind the `ENABLE_NEXT_DOCKER_RELEASE` feature flag; errors clearly if off. |
| `set_docker_autostart` | `entries`, `order?`, `persist_user_preferences`, `confirm` | `entries`: non-empty list of `{id, auto_start, wait?}`; validated locally. Upstream replaces the whole autostart list, so the tool reads the current list, merges your changes (order kept, new enables appended) and sends the full list; unknown ids rejected. Calls are serialised. `order`: ids to boot first (must be enabled after the change); the rest keep their relative order after them. |
| `vm_power` | `vm_id`, `action` ∈ `start`/`pause`/`resume`, `confirm` | id from `list_vms`. |
| `stop_vm` | `vm_id`, `confirm` | Graceful shutdown. |
| `reboot_vm` | `vm_id`, `confirm` | Reboot. |
| `force_stop_vm` | `vm_id`, `confirm` | **Hard power off** — may lose unsaved guest state. |
| `reset_vm` | `vm_id`, `confirm` | **Hard reset** (like the reset button) — unsaved guest state is lost. |
| `notification_archive` | `notification_id`, `action` ∈ `archive`/`unarchive`, `confirm` | `archive` clears one unread notification; `unarchive` moves an archived one back to unread. |
| `archive_all_notifications` | `importance=None`, `confirm` | Bulk archive (optionally one severity). |
| `delete_notification` | `notification_id`, `notification_type`, `confirm` | **Permanent.** `notification_type` ∈ `UNREAD`/`ARCHIVE` (where it currently lives). |
| `notification_archive_bulk` | `ids`, `action` ∈ `archive`/`unarchive`, `confirm` | Bulk archive / move back to unread by id (non-empty list, from `list_notifications`). |
| `unarchive_all_notifications` | `importance=None`, `confirm` | Unarchive everything (optionally one severity). |
| `delete_archived_notifications` | `confirm` | **Permanent.** Deletes every archived notification in one call. |
| `create_notification` | `title`, `subject`, `description`, `importance`, `link=None`, `confirm` | **Agent→operator channel.** Posts a notification into the Unraid WebGUI bell so you can leave the operator a persistent message (e.g. "disk 2 SMART errors climbing"). `importance` ∈ `INFO`/`WARNING`/`ALERT` (required). |

### Dangerous (only if the operator enabled **both** mutations and dangerous; **all require `confirm=true`**)

These are high-blast-radius. They appear only when `UNRAID_MCP_ALLOW_DANGEROUS=true`
*and* `UNRAID_MCP_ALLOW_MUTATIONS=true`. If they're absent, the operator has not
opted in — do not try to work around it.

| Tool | Args | Notes |
|------|------|-------|
| `mount_array_disk` | `disk_id`, `confirm` | id from `list_disks`. Brings one array disk online. |
| `unmount_array_disk` | `disk_id`, `confirm` | **Data becomes inaccessible** until remounted. |
| `clear_disk_statistics` | `disk_id`, `confirm` | **Unrecoverable** — resets that disk's read/write/error counters. |
| `add_disk_to_array` | `disk_id`, `slot=None`, `confirm` | **Array must be stopped.** Assigning a data slot can overwrite/format the disk once started. |
| `remove_docker_container` | `container_id`, `with_image=False`, `confirm` | **Permanent.** `with_image=true` also deletes the underlying image. |
| `update_all_docker_containers` | `confirm` | **Fleet-wide.** Pull + recreate **every** container with an available update — restarts many services at once. Prefer `update_docker_container(s)` for a specific target. |

> There is intentionally **no host reboot/shutdown** tool — the Unraid GraphQL API doesn't expose it.

---

## Conventions

- **IDs (`PrefixedID`).** The API returns ids like `"<serverId>:<rawId>"`. Pass back
  exactly what a `list_*` tool gave you. `get_docker_container` also accepts a plain name.
- **Sizes.** `{"bytes": int|null, "human": str|null}`. Array/share sizes derive from
  KiB; physical disk sizes from bytes — both are normalized to this shape for you.
- **Read results: text vs structured content.** A read tool returns one text block
  of compact JSON (no indentation; a list tool's text is one JSON array). In that
  text, keys with `null` values are omitted, so a missing key means "null", not
  "unsupported". List positions are preserved: `null` list elements stay in place
  (e.g. per-core CPU usage), and empty lists/objects are kept. `run_graphql_query`
  text is compact but unpruned (raw upstream data, nulls kept). `structuredContent`
  is the canonical payload: it keeps every field, nulls included, and matches the
  tool's `outputSchema` (list tools wrap it as `{"result": [...]}`). Error results
  and mutation results are unchanged.
- **Mutation results.** State-changing tools return a concise result, not the raw
  GraphQL envelope. Three shapes:
  - **Boolean actions** — parity (`start`/`pause`/`resume`/`cancel`) and VM
    (`start`/`stop`/`pause`/`resume`/`reboot`/`force_stop`) return `{"ok": true}`
    (or `{"ok": false}` if the server reported failure).
  - **Array start/stop** — `start_array`/`stop_array` return `{"state": "...", ...}`,
    where `start_array` also includes `capacity` normalized to `{bytes, human}`
    (just like `get_array_status`).
  - **Object-returning ops** — `docker_container_power`, `update_docker_container`,
    `notification_archive`, `archive_all_notifications`, `delete_notification`,
    `notification_archive_bulk`, `unarchive_all_notifications`,
    `delete_archived_notifications`, etc. return the flattened payload (e.g. the
    affected container, or `{unread, archive}` counts). `create_notification`
    returns the created `Notification` object (id, title, subject, description,
    importance, link, type, timestamp).
  - **List-returning ops** — `update_docker_containers` / `update_all_docker_containers`
    return a **list** of the recreated containers (`{id, names, state, status}` each);
    an empty list means nothing had an update to apply.
- **Disk health words** (on array disks): `healthy`, `failed`, `missing`, `new`,
  `unknown`, `empty`. Disk space warning/critical thresholds are not alarm flags.
- **Enums you'll see:** array `state` `STARTED|STOPPED|...`; container `state`
  `RUNNING|PAUSED|EXITED`; VM `state` `RUNNING|SHUTOFF|PAUSED|...`; notification
  `importance` `INFO|WARNING|ALERT`, `type` `UNREAD|ARCHIVE`.

---

## Recipes

**Triage "is my server healthy?"**
1. `get_health_summary`. If `overall == "ok"`, report and stop.
2. For `critical` or `attention`, read `reasons` and inspect the relevant disks,
   UPS, parity check, or unread notifications with the detailed tools.
3. For `degraded`, inspect `checks` and resolve the failed queries before
   concluding that the server is healthy.

`get_health_summary` and `unraid://health` return the same structure. Existing
fields remain available, with two additions: `reasons` lists human-readable
signals and failed checks, and `checks` reports `ok` or `failed` for each of
`array`, `ups`, `notifications`, and `temperature`. UPS also reports `not_configured` only when its
query fails with a plain GraphQL error (not a `FORBIDDEN`/`UNAUTHENTICATED` code, HTTP 403,
or unsupported field) and `upsConfiguration.service` is not `enable` (null or
`disable`). This adds no reason and does not cause a degraded verdict. Otherwise
the UPS check stays `failed`. HTTP 403 on the UPS or notifications sub-check
marks it `failed` (the array query already proved the key valid); connection
errors on any query propagate, as do auth errors on the array query.

An empty UPS or notification response is `ok`; a null or missing array is
`failed`. GraphQL errors, including partial errors, per-field permission denial,
and unsupported queries, mark a check `failed` while preserving usable data.
Authentication, connection, and configuration failures return actionable errors.
Check status describes query success, not component health.

The `overall` verdict uses this precedence:

- `critical`: a red disk indicator, failed/disabled or missing assigned disk,
  UPS `LOWBATT`, UPS `ONBATT` with charge below 20% or runtime below 300 seconds,
  or a temperature sensor at `critical`.
- `attention`: other unhealthy disks, unread alerts or warnings, a UPS on
  battery at any charge, parity-check errors greater than zero, or a temperature
  sensor at `warning`.
- `degraded`: at least one failed check and no critical or attention signal.
- `ok`: no health signals or failed checks, with `reasons == []`.

UPS status is split on whitespace and uppercased. `ONBATT` signals battery
operation; `LOWBATT` is critical regardless of charge or runtime. The documented
`On Battery` and `Low Battery` wording is also accepted. Missing charge or
runtime values do not trigger their numeric thresholds. Parity errors from the
last completed check keep the verdict at attention until the next check.
Temperature: the `temperature` section (`hottest` sensor name/value/unit/level plus
`warning_count` and `critical_count`) is omitted when the temperature check failed
(older API builds without per-sensor status/thresholds, or a permission error)
and no usable partial data came back; the check is still `failed` then.
The level is upstream's `current.status`, derived from the thresholds only when
that is absent; thresholds come from the upstream API's own configuration.
Upstream `type` is guessed from the sensor name and lm_sensors reports fans,
voltages, power and energy in CELSIUS (with spurious `CRITICAL` status), so
sensors are selected by id instead: lm_sensors ids (`<chip>:<label>:<key>`) count
only when the key is `temp<N>_input`; other ids (`disk:...`, `ipmi:...`) count.
Without an id, non-`CUSTOM` types or names containing "temp" count. On Super-I/O
hwmon chips only (lm_sensors chip name starting `nct`, `it8`, `w83` or `f71`),
readings (converted to C) at or below -40, or exactly 115.5, 127, 128 or 255, are
ignored as disconnected pins; every other source (IPMI, GPU, CPU, disk, NVMe) is
never filtered this way. Any other bogus pin belongs in the ignore list below. An NVMe sensor at upstream `critical` raises `attention` below 75 C and
`critical` at or above it: the upstream default NVMe critical is 60 C, which NVMe
drives routinely reach under load, so a lower reading would flap. CPU, HDD and
other sensors at `critical` raise `critical`.
Sensors listed in `UNRAID_MCP_HEALTH_IGNORE_SENSORS` (label, name or id) are left out
of the verdict and counted in `temperature.ignored_count`. Each warning or
critical sensor adds a reason such as `Temperature critical: disk1 65°C` (hottest
first, at most 5 per level).
Array state remains informational; a stopped array alone does not raise the
verdict. Failed checks remain visible even when a health signal takes precedence.

**Restart a container the user named "plex"** (mutations enabled)
1. `get_docker_container("plex")` → read its `id`.
2. Tell the user you're about to restart it. On confirmation:
   `restart_docker_container(container_id="<id>", confirm=true)`.

**Find what's filling the array**
1. `list_shares` → sort by `used.bytes` desc; report the largest with `used.human`.

**Check a parity check's progress**
1. `get_parity_status` → report `progress`, `speed`, `errors`, running/paused.

**A field has no dedicated tool** (only if `run_graphql_query` is enabled)
- `run_graphql_query("query { ... }")`. Mutations are rejected — use the typed
  mutation tools for changes.

---

## Errors you may get back

- *"Authentication failed … check UNRAID_API_KEY"* → the key is wrong or lacks the
  role/permission for that operation. Report it; don't retry blindly.
- *"Could not connect to Unraid at <host>"* → the server is unreachable or
  `UNRAID_API_URL` is wrong. Report it.
- *"Refusing to … without explicit confirmation"* → re-call with `confirm=true`
  **only if the user asked for that action**.
- *"GraphQL error: …"* → the query/field isn't available on this Unraid build;
  fall back to a related tool or `run_graphql_query`.

Secrets (the API key) are never present in any tool output or error — don't ask
for them and don't try to read them.

---

## Drop-in system-prompt snippet

> You have an `unraid` MCP server for monitoring and managing an Unraid server.
> Use `get_health_summary` for triage. All sizes are `{bytes, human}`. State-changing
> tools require `confirm=true` and only exist if the operator enabled mutations —
> only use them when the user explicitly asks, and summarize the impact of
> destructive actions (`stop_array`, `force_stop_vm`, `reset_vm`,
> `delete_notification`, correcting parity checks) before proceeding.
