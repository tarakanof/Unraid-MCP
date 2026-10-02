# Changelog

## Unreleased

### Changed

- **BREAKING: container ids are short in output (#172).** Container results
  (`list_docker_containers`, `get_docker_container`, logs, stats, port
  conflicts, docker mutation results and the `set_docker_autostart` list) show
  the 12-hex Docker short id instead of the 129-char `<serverId>:<64-hex>`
  `PrefixedID`. Only `list_docker_containers` and `check_docker_updates` check
  short ids against every container and fall back to the bare 64-hex id when
  two share one; other results always show 12 chars, and an ambiguous id is
  rejected on input with the candidates listed. Scripts that compare or store full ids must switch to the short or
  bare form. The live concise running-container list (30 containers) went from
  8,591 to 5,082 chars, the full list from 13,922 to 10,413, and port conflicts
  from 2,029 to 742.
- Container tools accept the short id (12+ hex chars, case-insensitive), the
  bare 64-hex id or the full `PrefixedID`; `get_docker_container` still also
  accepts a name. Upstream `PrefixedID` input takes the bare id but matches only
  the exact full id, so a short id is expanded with a container-list query.
  Mutations do that lookup only after `confirm` (and the destructive-tool
  elicitation), and the consequence text shows the id as the caller typed it. A
  short id that matches more than one container is an error that lists the
  candidates; no mutation is sent. Batch updates reject the same container
  given twice (any mix of short, bare and full ids) before sending anything.
  Ids are trimmed and their hex lowercased before they are sent.
- `check_docker_updates` results include the container's short `id` (joined
  by name in the same request), so its output can feed the update tools.

## 0.10.0 - 2026-10-02

LLM token-efficiency release (#163): smaller `tools/list` (read-only 40.1k →
30.5k chars; mutations-enabled 61.9k → 44.4k chars, 61 → 53 tools) and much
smaller tool results.

### Security

- Validation errors for tool arguments no longer echo input values, and
  argument names are redacted against the configured API key and bearer
  token (#162).

### Added

- Server `instructions` map tasks to tool groups for clients with tool search
  (Claude Code loads only names + instructions up front);
  `get_health_summary`, `list_warnings_and_alerts` and `get_system_info` carry
  `_meta["anthropic/alwaysLoad"]` (#161).

### Changed

- **BREAKING: unknown tool arguments are rejected (#162).** Every input schema
  publishes `additionalProperties: false` and calls with undeclared arguments
  fail with `Unknown argument. Allowed: …` before any request is made
  (previously they were silently ignored). `tools/list` is now in alphabetical
  order.

- **Read tools return one compact text block (#156).** The text block is
  compact JSON with null-valued keys omitted (list positions and empty
  containers kept); lists are one block instead of one per item.
  `structuredContent` is unchanged and canonical. `run_graphql_query` text is
  compact but keeps nulls.

- **Published schemas and descriptions trimmed (#157).** Auto-generated
  titles stripped, nullable `anyOf` collapsed where equivalent, redundant
  defaults dropped, docstrings unwrapped; ~30% smaller `tools/list` with
  identical validation.

- **Log and raw-query results have a 60,000-char budget (#159).**
  `get_docker_container_logs` keeps the newest lines (`truncated`,
  `omitted_lines`, `hint`; older lines are not retrievable — lower `tail`);
  `read_log_file` keeps leading lines and returns `next_start_line` to page on
  (`line_truncated` when a single line is cut); `run_graphql_query` returns a
  truncated preview with guidance to narrow the selection. Log tools carry
  `_meta["anthropic/maxResultSizeChars"]`.

- **BREAKING (default output): list tools take filters and default to
  `detail="concise"` (#158).** `list_docker_containers`, `list_disks` and
  `list_shares` now return a small key set per item by default. Pass
  `detail="full"` for the previous output (unchanged). `list_vms` gains
  `name`/`state` filters only (its output is already id/name/state). Concise keys:

  | Tool | Concise keys | Filters |
  | --- | --- | --- |
  | `list_docker_containers` | `id, name, image, state, status, update_available, web_ui_url` | `name`, `state` (`RUNNING`/`PAUSED`/`EXITED`), `update_available` |
  | `list_disks` | `id, name, device, type, smart_status, temp_c, spinning, size` | `name` (model or device), `type`, `smart_status` (`OK`/`UNKNOWN`) |
  | `list_vms` | no `detail` (always `id, name, state`) | `name`, `state` (`VmState`) |
  | `list_shares` | `name, free, used, size` | `name` |

  `name` filters are case-insensitive substrings; enum filters accept any case
  (`state="running"`). `update_available=false` also excludes containers whose
  value is `null` (older API builds). Invalid enum values are rejected with the
  allowed values before any request is made. `get_docker_container` / `get_disk`
  output contracts are unchanged. Agents or
  scripts that read `ports`, `names`, `auto_start`, `serial`, `interface`,
  `comment`, `allocator`, `cache` etc. from these lists must pass
  `detail="full"`.

- **BREAKING: non-destructive mutation tools consolidated into `action`-dispatch
  tools (#160).** Actions that share one module and one annotation tier
  (`MUTATING_IDEMPOTENT`) now live in a single tool taking
  `action: Literal[...]`, cutting the mutations-enabled `tools/list` from 61 to
  53 tools. Destructive tools are unchanged and keep their own names so hosts
  can still gate them. No aliases; update agent prompts and allowlists:

  | Old tool | New call |
  | --- | --- |
  | `start_docker_container(container_id)` | `docker_container_power(container_id, action="start")` |
  | `pause_docker_container(container_id)` | `docker_container_power(container_id, action="pause")` |
  | `unpause_docker_container(container_id)` | `docker_container_power(container_id, action="unpause")` |
  | `start_vm(vm_id)` | `vm_power(vm_id, action="start")` |
  | `pause_vm(vm_id)` | `vm_power(vm_id, action="pause")` |
  | `resume_vm(vm_id)` | `vm_power(vm_id, action="resume")` |
  | `pause_parity_check()` | `parity_check_control(action="pause")` |
  | `resume_parity_check()` | `parity_check_control(action="resume")` |
  | `cancel_parity_check()` | `parity_check_control(action="cancel")` |
  | `archive_notification(notification_id)` | `notification_archive(notification_id, action="archive")` |
  | `mark_notification_unread(notification_id)` | `notification_archive(notification_id, action="unarchive")` |
  | `archive_notifications(ids)` | `notification_archive_bulk(ids, action="archive")` |
  | `unarchive_notifications(ids)` | `notification_archive_bulk(ids, action="unarchive")` |

  All still require `confirm=true`, with the same refusal text per action.

## 0.9.0 - 2026-10-02

### Security

- UNRAID_API_KEY is validated at startup (>=32 chars, no placeholder); redaction ignores secrets shorter than 8 chars (#152).

### Added

- **`UNRAID_MCP_HEALTH_IGNORE_SENSORS`**: comma-separated temperature sensors
  (label, full name or id, case-insensitive) the health verdict ignores, with
  `temperature.ignored_count`. `get_system_metrics` still reports them (#151).

### Changed

- **`get_health_summary` / `unraid://health` now include temperature.** A fifth
  concurrent check (`checks.temperature`) feeds sensor levels into the verdict:
  a critical sensor gives `critical` (NVMe criticals below 75 C give `attention`), a warning
  sensor gives `attention`, plus a `temperature` section (hottest sensor and
  warning/critical counts). Only real temperature sensors count (lm_sensors
  fans/voltages/power and sentinel pin readings are ignored). On older APIs
  without per-sensor `metrics.temperature` status/thresholds the check fails, so
  health may now report `degraded` there (#151).

## 0.8.1 - 2026-10-01

### Fixed

- **Container stats could turn collected samples into an error.** Under an
  event-loop stall, coincident timers in the subscription sampler converted a
  partial result into "exceeded its sampling deadline". Every await in the
  stats and Docker-update paths now has its own `asyncio.timeout` bound. Caller
  cancellation always propagates, including around the Python 3.11 `wait_for`
  bug. A blocked progress callback or connection close can no longer hang a
  tool, and buffered frames can't overrun the sampling deadline (#144).

## 0.8.0 - 2026-10-01

Audit release (epic #121): fixes from a Codex code review, gaps against MCP spec
2026-07-28 / SDK 2.2.0, and new features from upstream unraid/api v4.30–v4.37.

### Removed

- **BREAKING: `remove_disk_from_array` tool removed.** Upstream unraid/api
  retired the `removeDiskFromArray` mutation (PR #2068, v4.37.4) because direct
  disk removal is unsafe; removal now goes only through Core's storage workflow
  (evacuation, parity-preserving shrink, checkpoints). Use the Unraid webGUI
  storage workflow instead (#99).

### Changed

- **BREAKING: `UNRAID_MCP_BEARER_TOKEN` is now required on non-localhost binds.**
  Previously the server generated a token and logged it to stderr; on
  `0.0.0.0` (Docker) anyone able to read container logs could authenticate.
  It now exits non-zero with an actionable message instead. Localhost binds
  keep the generate-and-log-once dev convenience (#101).
- **Health verdict** (`get_health_summary`, `unraid://health`): `overall` is now
  `ok | attention | critical | degraded`, with `reasons` and per-sub-check
  `checks` (`ok | failed | not_configured`). It reflects UPS on battery
  (real apcupsd `ONBATT`/`LOWBATT` statuses), parity errors and failed or
  forbidden sub-queries. Disk health is status-based: free-space thresholds no
  longer raise false "critical" alarms. A box with no UPS configured reports
  `ups: not_configured`, not a failure. Unread warnings/alerts surface as
  `top_alerts` (#104, #119).
- **Destructive tools ask the human** via MCP elicitation when the client
  supports it (2026-07-28 clients, or legacy clients over stdio); `confirm=true`
  is still required in every mode (#113).
- Tools have human-readable `title`s and `idempotentHint` annotations; the
  server advertises `title`, `website_url` and icons (#111).
- Core tools (health, containers, disks) advertise typed `outputSchema`s (#112).
- Independent health, plugin and system-info queries run concurrently (#110).
- Long-running Docker updates and array start/stop use a longer read timeout
  (`UNRAID_MCP_LONG_TIMEOUT`, default 600 s); a timeout says the operation may
  still be running (#103). Batch updates and container stats emit progress
  notifications (#114).

### Added

- `get_hardware_inventory`: GPU, PCI, USB and network devices (#118).
- Network throughput in `get_system_metrics` (`metrics.network`) (#115).
- Richer container fields, `get_docker_port_conflicts`, and opt-in container
  sizes (`get_docker_container(include_sizes=true)`, slow) (#116).
- Temperature thresholds/levels and hottest sensor, UPS power, array boot
  devices, and extra ArrayDisk fields (#117).
- `list_warnings_and_alerts` (#119).
- Mutations: `refresh_docker_digests` and `set_docker_autostart`. Autostart is
  read-merge-write and serialized, because upstream replaces the whole list (#120).

### Fixed

- **Security:** API key and bearer token are scrubbed from returned GraphQL
  data, websocket frames, every exception message and log record. Raw websocket
  frame debug logging is suppressed (#100).
- Failed mutations no longer report success. The GraphQL envelope is validated,
  and malformed responses map to actionable errors (#102).
- Websocket sends and unsubscribe are bounded by the sampling deadline (#105).
- The container HEALTHCHECK honors TLS and ignores proxy env vars (#106).
- `read_log_file` validates path containment and numeric bounds before any
  request; `start_line` is 1-based (#107).
- Disk partition sizes use the `{bytes, human}` shape (#108).

### Tests

- Offline, auto-discovered mutation-refusal and read-tool contract tests (#109).

## 0.7.0 - 2026-08-06

MCP spec 2026-07-28 adoption (epic #79): SDK v2, stateless HTTP, cache hints.
Pre-2026 clients remain fully supported (the legacy `initialize` handshake is
served and negotiates the client's requested protocol revision).

### Changed

- **Migrated to `mcp` SDK v2 (2.0.0)** — the MCP 2026-07-28 specification
  baseline (#75). Mostly internal (`FastMCP` → `MCPServer`, transport wiring
  moved out of the constructor); behavior, tools, configuration, and the
  security model are unchanged. `serverInfo.version` now reports the
  unraid-mcp release version instead of the SDK's.
- **Streamable HTTP is stateless** (#76): every request is self-contained —
  no `Mcp-Session-Id`, no session affinity needed behind a reverse proxy or
  load balancer, and restarting the container between client requests is safe.
  Pre-2026 clients still work: their `initialize` is answered (sessionless).
  No new configuration.

### Added

- **Cache hints** (`ttlMs`/`cacheScope`, spec 2026-07-28) on cacheable
  responses (#77): 5 min for `tools/list`, `prompts/list`, `resources/list`,
  `resources/templates/list`, and `server/discover` (the registered set is
  fixed per process); 10 s for `resources/read` (health/system-info are
  point-in-time snapshots), scoped `private` so shared caches never serve one
  client's snapshot to another. Additive: clients that ignore the hints see no
  change.

### Fixed

- IPv6 binds (`UNRAID_MCP_HOST=::1`) no longer reject every request with
  `421 Misdirected Request` — the DNS-rebinding Host allow-list now uses the
  bracketed `[::1]:port` form (pre-existing bug surfaced by the migration
  review, #80).
- Resource read errors carry a machine-readable `data.uri` field, matching
  SDK-generated resource errors (#80).

### Dependencies

- `mcp` 1.28.1 → 2.0.0 (pulls in `mcp-types` and `httpx2`; our own GraphQL
  client stays on `httpx`).
- `cryptography` pinned ≥ 50.0.0 (CVE-2026-69247 / PYSEC-2026-3552; was a
  pre-existing transitive dependency resolved at a vulnerable version).

## 0.6.0 - 2026-07-04

Resources, prompts, and live per-container stats (milestone v0.6.0). Completes
the initial capability roadmap.

### Added

- MCP **resources**: `unraid://health` and `unraid://system-info` — same JSON
  as the matching read tools, readable without spending a tool call; clean,
  secret-free error when the box is unreachable (#26).
- MCP **prompt** `triage` (optional `focus` argument) — walks an agent
  top-down from `get_health_summary` into whichever subsystem needs attention;
  never runs mutating tools without operator confirmation (#26).
- `get_docker_container_stats` — one-shot sample of the `dockerContainerStats`
  GraphQL **subscription** over `graphql-transport-ws`: per-container CPU%,
  memory%, and mem/net/block I/O in a single bounded call (~2s typical, 12s
  hard cap, never hangs). Includes control-character sanitization of upstream
  `docker stats` output and TLS/proxy parity with the HTTP client; the API key
  travels only in `connection_init` and never appears in errors or logs
  (#65, investigation #27).
- `list_plugins` — installed Unraid plugins from the `plugins` and
  `installedUnraidPlugins` queries, unioned and source-tagged (#28).

### Fixed

- Subscription sampler: keyless `next` frames are skipped instead of being
  mistaken for the cycle-repeat signal, which could silently truncate a stats
  snapshot (#66).

### Dependencies

- New runtime dependency: `websockets>=13` (pure Python, no transitive deps).

## 0.5.0 - 2026-07-04

Safe mutation expansion (milestone v0.5.0) on top of the observability tools
added since 0.3.0.

### Added — safe mutation expansion

- Tiered mutation permissions: a third **dangerous** tier behind
  `UNRAID_MCP_ALLOW_DANGEROUS` (only effective alongside
  `UNRAID_MCP_ALLOW_MUTATIONS`), housing high-blast-radius array-topology ops
  (`mount_array_disk`, `unmount_array_disk`, `clear_disk_statistics`,
  `add_disk_to_array`, `remove_disk_from_array`) and `remove_docker_container`
  (#25).
- Docker: native `restart_docker_container` (atomic, with a stop→start fallback
  on older API builds) plus `pause_docker_container` / `unpause_docker_container`
  (#21).
- Docker updates: `update_docker_container` and `update_docker_containers`
  (batch, capped at 20) in the mutate tier; `update_all_docker_containers` in
  the dangerous tier (#22).
- VM: `reset_vm` — hard reset, like the physical reset button (#23).
- Notifications: bulk `archive_notifications` / `unarchive_notifications`,
  `unarchive_all_notifications`, `delete_archived_notifications`, and
  `create_notification` — an agent→operator channel that posts a persistent note
  into the Unraid WebGUI (#24).
- Every mutating tool keeps its `MUTATING`/`DESTRUCTIVE` annotation and refuses
  without `confirm=true` before any network I/O.

### Added — observability tools

- `get_system_metrics` — live CPU / memory / temperature utilization (#52).
- `get_docker_container_logs` — paged, capped container log retrieval (#53).
- `list_log_files` + `read_log_file` — system log browsing, paged and
  size-capped (#54).
- `get_services`, `check_docker_updates`, and native container lookup (#55).
- `get_system_time`, flash device identity, and richer share fields (#56).
- API capability detection foundation, so tools unsupported by the box's API
  version degrade gracefully (#51).

### Maintenance

- Dependabot bumps for GitHub Actions, Python, and Docker base image; CI git
  identity fix for annotated release tags.

## 0.3.0 - 2026-07-03

### Fixed

- `get_disk` raises a typed `ToolError` on not-found instead of returning null
  (#11); empty array slots (`DISK_NP`) no longer counted as unhealthy (#14);
  `list_vms` retries with the legacy `domain` field on older API builds (#13).
- Bearer tokens compared as bytes — non-ASCII `Authorization` header now
  returns a clean 401 (#10); clear, actionable error on 3xx redirects without
  leaking the URL (#12).

### CI / tests

- Weekly schema-drift check for `queries.py` against the upstream schema (#31);
  env-gated live smoke suite (`pytest -m live`) (#32); Dependabot config (#33).

## 0.2.1 - 2026-06-11

### Security hardening

- All GitHub Actions are pinned to commit SHAs and the Trivy scanner image to
  its digest; the CI workflow token is now read-only.
- Secrets are scrubbed from formatted log output including exception
  tracebacks, which logging filters never see.
- The bearer-auth middleware rejects websocket connections with a proper
  close frame (code 1008) instead of HTTP frames.
- Docker Compose and the Unraid template now run the container with
  `no-new-privileges`, all capabilities dropped, and a read-only root
  filesystem.
- Bumped the `python:3.12-alpine` base image for OpenSSL CVE-2026-45447.

### Fixed

- The container healthcheck probes the configured `UNRAID_MCP_HOST` instead
  of hardcoded `127.0.0.1`.

### Documentation

- `docs/security.md` notes that tool output is untrusted data (prompt
  injection via upstream strings such as notification text).
- README documents installing via the Unraid template.

## 0.2.0 - 2026-06-02

### Security hardening

- Docker and Compose deployments now verify TLS to the Unraid API by default.
- Operator-supplied HTTP bearer tokens must be at least 32 random characters and
  cannot be common placeholders. Existing short or placeholder tokens now fail
  startup and must be replaced.
- Outbound Unraid API requests ignore ambient proxy environment variables.
- The Docker image uses locked runtime dependencies and removes build tooling
  from the final image.

### Operations

- Added CI security checks for Bandit, pip-audit, Trivy config scanning, and
  Trivy image scanning.
