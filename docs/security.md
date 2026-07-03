# Security model

The server is built to be safe to point at a real Unraid box by default. The short
version: it can't change anything unless you opt in, it can't leak your API key, and
the network transport is locked down.

## Read-only by default

Monitoring tools are always available. State-changing tools are registered **only**
when `UNRAID_MCP_ALLOW_MUTATIONS=true`. Even then, **every** mutating tool requires an
explicit `confirm=true` and refuses *before* making any network call without it — so
an agent can't change anything by accident, and a refusal never touches your server.

There is intentionally **no host reboot/shutdown** tool: the Unraid GraphQL API
doesn't expose those mutations, so neither does this server.

## Permission tiers

Tools are grouped into three tiers, each behind its own flag. The tiers are
cumulative and the dangerous tier is gated by the mutations flag on top of its own —
enabling `UNRAID_MCP_ALLOW_DANGEROUS` *without* `UNRAID_MCP_ALLOW_MUTATIONS` unlocks
nothing.

| Tier | Flag | Default | Unlocks |
| --- | --- | --- | --- |
| Read | *(always on)* | on | All monitoring/read tools. Never change anything. |
| Mutate | `UNRAID_MCP_ALLOW_MUTATIONS` | off | Everyday writes: start/stop array, start/pause/resume/cancel parity, start/stop/restart Docker containers, start/stop/pause/resume/reboot/force-stop VMs, notification archive/unread/delete. |
| Dangerous | `UNRAID_MCP_ALLOW_DANGEROUS` (requires mutations too) | off | High-blast-radius topology/removal ops (see below). |

**Dangerous-tier tools** (all annotated `destructive`, all require `confirm=true`):

- `mount_array_disk` — bring one array disk online.
- `unmount_array_disk` — take one array disk offline; its data becomes inaccessible until remounted.
- `clear_disk_statistics` — reset a disk's read/write/error I/O counters (unrecoverable).
- `add_disk_to_array` — assign a physical disk to the array (array must be stopped; can overwrite/format the disk once started).
- `remove_disk_from_array` — drop a disk from the array config (array must be stopped; data becomes inaccessible).
- `remove_docker_container` — permanently delete a container, and optionally (`with_image=true`) its underlying image.

Splitting these out means you can safely hand an agent everyday container/array
control without also handing it the ability to reshape the array or delete
containers — those stay locked until you deliberately opt into the dangerous tier.

## Least privilege

Use a scoped Unraid API key. A `guest`/read key is enough for all the read-only
tools — only create a wider-scoped key if you actually enable mutations.

## Secrets stay secret

The API key is held as a `SecretStr`, never logged, and never appears in tool output
or error messages. A redaction filter scrubs it (and any generated bearer token) from
all log lines as defence in depth.

## stdio is clean

All logs go to **stderr**; stdout carries only the JSON-RPC protocol. The default
`stdio` transport has no network surface at all.

## The HTTP transport is gated

When you run `streamable-http`:

- It binds `127.0.0.1` by default.
- It requires a bearer token, compared in constant time. Requests with a missing or
  duplicated `Authorization` header are rejected.
- Operator-supplied bearer tokens must be at least 32 characters and cannot be
  common placeholders such as `change-me`.
- **DNS-rebinding protection** (Host/Origin validation) is on automatically for
  localhost binds. For a non-localhost bind, set `UNRAID_MCP_ALLOWED_HOSTS` to keep
  it on — the server warns if you don't.
- **TLS:** set `UNRAID_MCP_TLS_CERT` + `UNRAID_MCP_TLS_KEY` to serve HTTPS directly,
  or terminate TLS at a reverse proxy. The server warns loudly if it's serving
  plaintext on a non-localhost address. Don't expose it to untrusted networks.

## Outbound API path

Requests to the Unraid API verify TLS by default and ignore ambient proxy
environment variables such as `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`. That
keeps API traffic from being silently redirected through process-level proxy
settings; route the container or host directly to the Unraid API endpoint.

## Tool output can include hardware identifiers

`get_system_info` includes the boot flash device's GUID (`flash.guid`), which is
also the identifier Unraid ties your license to. That's expected for a tool
talking to your own box, but keep it in mind if you ever relay tool output
somewhere less trusted than your own agent session.

## Tool output is untrusted data

Tool results echo strings that originate on the Unraid box — container names, share
comments, notification titles/descriptions, **Docker container logs**, and **system
log content**. A hostile or compromised service there could plant prompt-injection
text in them — log output is workload-controlled and gets special mention because
it's often long, freeform, and easy to overlook as "just output". MCP clients and
agents should treat all tool output as data, never as instructions.

Container logs can also contain secrets that the user's own containers print (API
keys, tokens, connection strings). That's inherent to reading logs and not something
this server can filter — treat `get_docker_container_logs` output with the same care
as any other secret-bearing log stream.

## Log file access is restricted

`read_log_file` only accepts paths under `/var/log` — the prefix the Unraid API
serves system logs from — and rejects anything else with a `ToolError` before
making any network call, pointing the caller back to `list_log_files` for a valid
path. This is defense-in-depth on top of server-side validation, not a substitute
for it. `lines` is capped at 500 per call to bound response size; page through
larger files with `start_line`.

## No arbitrary execution

Only typed GraphQL operations are issued. The optional raw-query tool
(`UNRAID_MCP_ALLOW_RAW_QUERY=true`) parses the document and allows **only** `query`
operations — mutations and subscriptions are rejected, including ones hidden behind
comments or leading whitespace.
