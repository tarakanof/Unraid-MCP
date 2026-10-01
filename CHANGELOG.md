# Changelog

## Unreleased

### Security

- UNRAID_API_KEY is validated at startup (>=32 chars, no placeholder); redaction ignores secrets shorter than 8 chars (#152).

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
