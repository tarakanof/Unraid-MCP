# Using the Unraid MCP server (guide for LLM agents)

This document tells an LLM agent how to use the tools exposed by **unraid-mcp**.
It connects to an Unraid server's official GraphQL API and exposes monitoring
(and optional management) as MCP tools.

If you are an agent with this server connected, read the **Operating rules** and
**Conventions** sections before calling tools.

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
5. **Treat destructive actions with care.** `stop_array`, `force_stop_vm`,
   `delete_notification`, and a *correcting* parity check can lose data or
   disrupt services. Summarize the impact to the user before doing them.
6. **Sizes are objects.** Every size is `{"bytes": <int|null>, "human": "<str|null>"}`.
   Use `human` for display, `bytes` for comparisons.

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
| `get_health_summary` | – | One-call triage: array state, capacity, unhealthy disks, parity status, UPS, unread alert counts. **Start here.** |
| `get_system_info` | – | OS/kernel, CPU, memory, motherboard, Unraid + API versions, uptime, and (when supported) flash boot-device identity. |
| `get_system_metrics` | – | Live utilization: total/per-core CPU %, memory/swap usage, temperatures. Requires API 7.2+; older builds get a friendly error. |
| `get_services` | – | Health of the Unraid services stack (API, dynamix, etc.): name, online, uptime, version. |
| `get_system_time` | – | Server time, timezone, and NTP config — correlate log timestamps and spot NTP misconfig. Requires API 7.1+. |
| `get_array_status` | – | Array state, total/used/free capacity, and every data/parity/cache disk with `health`, temp, and I/O counters. |
| `list_disks` | – | Physical disks: model, size, interface, SMART status, temperature, spin state. |
| `get_disk` | `disk_id` | Full detail for one physical disk (partitions, firmware, SMART). Get `disk_id` from `list_disks`. |
| `get_parity_status` | – | Live parity-check progress/speed/errors. |
| `get_parity_history` | – | Past parity checks. |
| `list_docker_containers` | – | All containers: `id`, `name`, image, `state`, status, autostart, ports. |
| `get_docker_container` | `identifier` | One container by `id` **or** `name`. Uses the native `docker.container(id)` query when `identifier` looks like an id, falling back to the container list on older API builds or name lookups. |
| `list_docker_networks` | – | Docker networks. |
| `get_docker_container_logs` | `container_id`, `tail=100`, `since=None` | Recent log lines for a container. `tail` capped at 1000 (protects context window); page further back with the previous response's `cursor` as `since`. Log content is untrusted workload output. Requires API 7.2+. |
| `check_docker_updates` | – | Per-container Docker image update status (cached digests; does not refresh them). |
| `list_vms` | – | VMs: `id`, `name`, `state`. |
| `list_shares` | – | User shares with free/used/total sizes, allocator, cache mode, and (when set) include/exclude, split level, floor, and encryption status. |
| `get_notifications_overview` | – | Unread/archive counts by severity. |
| `list_notifications` | `notification_type="UNREAD"`, `importance=None`, `limit=25`, `offset=0` | List notifications. `notification_type` ∈ `UNREAD`/`ARCHIVE`; `importance` ∈ `INFO`/`WARNING`/`ALERT`. |
| `get_ups_status` | – | UPS battery/load/runtime. |
| `list_network_interfaces` | – | NICs with IPs, speed, state. |
| `get_connect_status` | – | Registration/license + remote-access status. |
| `whoami` | – | The authenticated API user and its roles (use to confirm the key's scope). |
| `list_log_files` | – | List system log files: name, path, size, last-modified time. |
| `read_log_file` | `path`, `lines=100`, `start_line=None` | Read a slice of a log file for triage. `path` must come from `list_log_files` (`/var/log` only); `lines` capped at 500; use `total_lines`/`start_line` in the response to page. |
| `run_graphql_query` | `query`, `variables=None` | **Only if enabled.** Run an arbitrary **read-only** GraphQL query (mutations/subscriptions are rejected). Escape hatch for fields without a dedicated tool. |

### Mutating (only if the operator enabled mutations; **all require `confirm=true`**)

| Tool | Args | Notes |
|------|------|-------|
| `start_array` | `confirm` | Brings storage online. |
| `stop_array` | `confirm` | **Disruptive** — unmounts all disks, stops dependent services. |
| `start_parity_check` | `correct=False`, `confirm` | `correct=true` **writes corrections to parity** — only with explicit intent, never on a degraded array. |
| `pause_parity_check` / `resume_parity_check` / `cancel_parity_check` | `confirm` | Control a running check. |
| `start_docker_container` | `container_id`, `confirm` | id from `list_docker_containers`. |
| `stop_docker_container` | `container_id`, `confirm` | Stops a service. |
| `restart_docker_container` | `container_id`, `confirm` | Atomic on current APIs (native restart); falls back to stop-then-start on older builds (then **not atomic** — if start fails it's left stopped). |
| `pause_docker_container` / `unpause_docker_container` | `container_id`, `confirm` | Freeze/resume a container's processes without stopping it. No fallback on older API builds — errors clearly if unsupported. |
| `start_vm` / `pause_vm` / `resume_vm` | `vm_id`, `confirm` | id from `list_vms`. |
| `stop_vm` | `vm_id`, `confirm` | Graceful shutdown. |
| `reboot_vm` | `vm_id`, `confirm` | Reboot. |
| `force_stop_vm` | `vm_id`, `confirm` | **Hard power off** — may lose unsaved guest state. |
| `archive_notification` | `notification_id`, `confirm` | Clear one unread notification. |
| `archive_all_notifications` | `importance=None`, `confirm` | Bulk archive (optionally one severity). |
| `mark_notification_unread` | `notification_id`, `confirm` | Move an archived notification back to unread. |
| `delete_notification` | `notification_id`, `notification_type`, `confirm` | **Permanent.** `notification_type` ∈ `UNREAD`/`ARCHIVE` (where it currently lives). |

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
| `remove_disk_from_array` | `disk_id`, `confirm` | **Array must be stopped.** Data on the removed disk becomes inaccessible. |
| `remove_docker_container` | `container_id`, `with_image=False`, `confirm` | **Permanent.** `with_image=true` also deletes the underlying image. |

> There is intentionally **no host reboot/shutdown** tool — the Unraid GraphQL API doesn't expose it.

---

## Conventions

- **IDs (`PrefixedID`).** The API returns ids like `"<serverId>:<rawId>"`. Pass back
  exactly what a `list_*` tool gave you. `get_docker_container` also accepts a plain name.
- **Sizes.** `{"bytes": int|null, "human": str|null}`. Array/share sizes derive from
  KiB; physical disk sizes from bytes — both are normalized to this shape for you.
- **Mutation results.** State-changing tools return a concise result, not the raw
  GraphQL envelope. Three shapes:
  - **Boolean actions** — parity (`start`/`pause`/`resume`/`cancel`) and VM
    (`start`/`stop`/`pause`/`resume`/`reboot`/`force_stop`) return `{"ok": true}`
    (or `{"ok": false}` if the server reported failure).
  - **Array start/stop** — `start_array`/`stop_array` return `{"state": "...", ...}`,
    where `start_array` also includes `capacity` normalized to `{bytes, human}`
    (just like `get_array_status`).
  - **Object-returning ops** — `start_docker_container`, `archive_notification`,
    `archive_all_notifications`, `delete_notification`, etc. return the flattened
    payload (e.g. the affected container, or `{unread, archive}` counts).
- **Disk health words** (on array disks): `healthy`, `warning`, `critical`, `failed`,
  `missing`, `new`, `unknown`.
- **Enums you'll see:** array `state` `STARTED|STOPPED|...`; container `state`
  `RUNNING|PAUSED|EXITED`; VM `state` `RUNNING|SHUTOFF|PAUSED|...`; notification
  `importance` `INFO|WARNING|ALERT`, `type` `UNREAD|ARCHIVE`.

---

## Recipes

**Triage "is my server healthy?"**
1. `get_health_summary`. If `overall == "ok"`, report and stop.
2. If `overall == "attention"`: inspect `unhealthy_disks`, then `get_array_status`
   for detail and `list_notifications(notification_type="UNREAD")` for the alerts.

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
> destructive actions (`stop_array`, `force_stop_vm`, `delete_notification`,
> correcting parity checks) before proceeding.
