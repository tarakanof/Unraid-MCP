# unraid-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes your **Unraid** server to MCP-client agents (Claude Desktop, the `hermes` harness, etc.) through Unraid's official **GraphQL API**.

It is **read-only by default** — monitoring tools are always available, while anything that changes the server is opt-in and confirmation-gated. Secrets are never logged, all logging goes to stderr, and `stdio` (the default transport) has no network surface.

## Features

**Read-only tools (always available):**

| Tool | What it returns |
|------|-----------------|
| `get_system_info` | OS/kernel, CPU, memory, motherboard, Unraid + API versions, uptime |
| `get_array_status` | Array state, capacity, every disk with health/temp/I-O, parity-check status |
| `list_disks` / `get_disk` | Physical disks: model, size, interface, SMART, temperature, spin state, partitions |
| `get_parity_status` / `get_parity_history` | Current and past parity checks |
| `list_docker_containers` / `get_docker_container` | Containers with state, image, ports, autostart |
| `list_docker_networks` | Docker networks |
| `list_vms` | Virtual machines and their state |
| `list_shares` | User shares with sizes, allocator, cache mode |
| `list_notifications` / `get_notifications_overview` | Notifications + unread/archive counts by severity |
| `get_ups_status` | UPS battery/load/runtime |
| `list_network_interfaces` | NICs with IPs, speed, state |
| `get_connect_status` | Registration/license + remote-access status |
| `whoami` | The authenticated API user and its roles |
| `get_health_summary` | One-call triage roll-up: array, unhealthy disks, parity, UPS, alerts |

**Mutating tools** (only registered when `UNRAID_MCP_ALLOW_MUTATIONS=true`). **Every mutating tool requires `confirm=true`** and refuses — before making any network call — without it. The **bold** ones are additionally flagged with a destructive hint for clients:

- Array: `start_array`, **`stop_array`**
- Parity: `start_parity_check`, `pause_parity_check`, `resume_parity_check`, `cancel_parity_check`
- Docker: `start_docker_container`, **`stop_docker_container`**, **`restart_docker_container`**
- VM: `start_vm`, **`stop_vm`**, `pause_vm`, `resume_vm`, **`reboot_vm`**, **`force_stop_vm`**
- Notifications: `archive_notification`, **`archive_all_notifications`**, `mark_notification_unread`, **`delete_notification`**

> There is intentionally **no reboot/shutdown of the host** — the Unraid GraphQL API does not expose those mutations, so neither does this server.

**Escape hatch:** `run_graphql_query` (only when `UNRAID_MCP_ALLOW_RAW_QUERY=true`) runs an arbitrary **read-only** GraphQL query; mutations and subscriptions are rejected.

## Prerequisites

- **Unraid 7.2+** (the API is built in) or an earlier version with the **Unraid Connect** plugin installed.
- An **API key**. Create one in the WebGUI (**Settings → Management Access → API Keys**) or on the server's terminal:

  ```bash
  # Least privilege for monitoring — a read/guest-scoped key is enough:
  unraid-api apikey --create --name mcp --roles guest
  ```

  Use a key with only the roles/permissions you need. A `guest`/read-scoped key is sufficient for the read-only tools.

## Install

```bash
git clone https://github.com/tarakanof/Unraid-MCP
cd Unraid-MCP
uv sync                 # or: pip install .
```

## Configure

Copy `.env.example` to `.env` and fill it in (or export the variables in the environment):

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNRAID_API_URL` | _(required)_ | GraphQL endpoint, e.g. `https://tower.local/graphql` (`/graphql` is appended if omitted) |
| `UNRAID_API_KEY` | _(required)_ | API key, sent as `x-api-key` |
| `UNRAID_VERIFY_SSL` | `true` | Verify TLS. See note below for self-signed certs |
| `UNRAID_CA_BUNDLE` | – | Path to a CA bundle to trust the Unraid certificate |
| `UNRAID_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `UNRAID_MCP_HOST` | `127.0.0.1` | Bind address for HTTP transport |
| `UNRAID_MCP_PORT` | `6750` | Port for HTTP transport |
| `UNRAID_MCP_BEARER_TOKEN` | – | Bearer token required from HTTP clients (auto-generated + printed if unset) |
| `UNRAID_MCP_ALLOWED_HOSTS` | – | Comma-separated Host allow-list for DNS-rebinding protection (HTTP). Set this for non-localhost binds |
| `UNRAID_MCP_ALLOWED_ORIGINS` | – | Comma-separated Origin allow-list for DNS-rebinding protection (HTTP) |
| `UNRAID_MCP_TLS_CERT` | – | TLS certificate (PEM) to serve the HTTP transport over HTTPS. Set with the key below |
| `UNRAID_MCP_TLS_KEY` | – | TLS private key (PEM). Both cert + key enable HTTPS directly |
| `UNRAID_MCP_ALLOW_MUTATIONS` | `false` | Register mutating tools (each still requires `confirm=true`) |
| `UNRAID_MCP_ALLOW_RAW_QUERY` | `false` | Register the read-only raw GraphQL tool |
| `UNRAID_MCP_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `UNRAID_MCP_LOG_LEVEL` | `INFO` | Log level (to stderr) |

> **Self-signed certificates:** Unraid local hosts often use self-signed TLS. Prefer pointing `UNRAID_CA_BUNDLE` at the certificate over setting `UNRAID_VERIFY_SSL=false`. Disabling verification logs a warning.

## Run

```bash
# stdio (default) — for local subprocess clients
unraid-mcp
# or
python -m unraid_mcp

# streamable-HTTP — for remote clients
UNRAID_MCP_TRANSPORT=streamable-http UNRAID_MCP_BEARER_TOKEN=$(openssl rand -hex 32) unraid-mcp
```

## Connect a client

**Generic stdio harness (e.g. `hermes`)** — launch the server as a subprocess and pass the connection via env:

```json
{
  "mcpServers": {
    "unraid": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/unraid-mcp", "unraid-mcp"],
      "env": {
        "UNRAID_API_URL": "https://tower.local/graphql",
        "UNRAID_API_KEY": "your-api-key"
      }
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`) uses the same `mcpServers` shape.

**Remote / HTTP**: point the client at `http://<host>:6750/mcp` and send `Authorization: Bearer <UNRAID_MCP_BEARER_TOKEN>`.

## Security model

- **Read-only by default.** Mutations require `UNRAID_MCP_ALLOW_MUTATIONS=true`, and then **every** mutating tool requires an explicit `confirm=true` and refuses *before* making any network call.
- **Least privilege.** Use a scoped Unraid API key; a read/guest key suffices for monitoring.
- **Secrets stay secret.** The API key is held as a `SecretStr`, never logged (a redaction filter scrubs it as defence in depth), and never appears in error messages.
- **stdio is clean.** All logs go to stderr; stdout carries only the JSON-RPC protocol.
- **HTTP is gated.** The HTTP transport binds `127.0.0.1` by default and requires a bearer token (constant-time compared; duplicate/absent `Authorization` headers are rejected). DNS-rebinding protection (Host/Origin validation) is enabled automatically for localhost binds; for a non-localhost bind set `UNRAID_MCP_ALLOWED_HOSTS` to keep it on. **TLS:** set `UNRAID_MCP_TLS_CERT` + `UNRAID_MCP_TLS_KEY` to serve HTTPS directly, or terminate TLS at a reverse proxy — the server warns loudly if it serves plaintext on a non-localhost address. Don't expose it to untrusted networks.
- **No arbitrary execution.** Only typed GraphQL operations are issued; the optional raw-query tool parses the document and allows only `query` operations (mutations/subscriptions are rejected — including ones hidden behind comments or leading whitespace).

## Development

```bash
uv sync --extra dev
uv run pytest          # 146 tests, all mocked — no live server needed
uv run ruff check .
uv run ruff format .
```

Tests mock the GraphQL endpoint with `respx`, so the suite runs without a real Unraid server. The architecture keeps logic functions pure and testable; the `@mcp.tool` wrappers are thin.

## Docker

```bash
docker build -t unraid-mcp .
docker run --rm -p 6750:6750 \
  -e UNRAID_API_URL=https://tower.local/graphql \
  -e UNRAID_API_KEY=your-api-key \
  -e UNRAID_MCP_BEARER_TOKEN=$(openssl rand -hex 32) \
  unraid-mcp
```

The image defaults to the streamable-HTTP transport bound to `0.0.0.0`; always set a bearer token and front it with TLS for anything beyond a trusted LAN.

## License

MIT — see [LICENSE](LICENSE).
