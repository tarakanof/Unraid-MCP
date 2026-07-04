<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/icons/unraid-mcp-lockup-dark.png">
    <img alt="Unraid MCP" src="docs/icons/unraid-mcp-lockup-light.png" width="460">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/tarakanof/Unraid-MCP/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/tarakanof/Unraid-MCP/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/tarakanof/Unraid-MCP/actions/workflows/security.yml"><img alt="Security" src="https://github.com/tarakanof/Unraid-MCP/actions/workflows/security.yml/badge.svg"></a>
  <a href="https://hub.docker.com/r/dtarakanov/unraid-mcp"><img alt="Docker Hub" src="https://img.shields.io/docker/v/dtarakanov/unraid-mcp?sort=semver&logo=docker&label=docker"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/github/license/tarakanof/Unraid-MCP"></a>
</p>

An [MCP](https://modelcontextprotocol.io) server that hooks your **Unraid** box up to
MCP-aware agents (Claude Desktop, the `hermes` harness, whatever) through Unraid's
official **GraphQL API**.

It's **read-only by default** — the monitoring tools are always on, and anything that
can change the server is opt-in and asks for confirmation. Your API key never gets
logged. The details are in [docs/security.md](docs/security.md).

> Needs Unraid 7.2+ (the API is built in) or the Unraid Connect plugin on older versions.

## What you get

Read-only tools for the stuff you'd actually want to check: system info, live CPU/
memory/temperature metrics, array and disk health, parity, Docker containers/networks,
live per-container CPU%/memory stats, VMs, shares, notifications, UPS, network
interfaces, and a one-shot `get_health_summary` for quick triage.

Opt-in mutating tools (start/stop the array, control Docker/VMs, run parity checks,
manage notifications) only show up when you set `UNRAID_MCP_ALLOW_MUTATIONS=true`, and
every one of them needs `confirm=true`. That includes `create_notification` — an
agent→operator channel that posts a persistent message straight into the Unraid
WebGUI's notification bell, so an agent that spots a problem can leave a note where
you'll actually see it.

A third **dangerous** tier (`UNRAID_MCP_ALLOW_DANGEROUS=true`) unlocks high-blast-radius
operations — array topology (add/remove/mount/unmount a disk, clear disk statistics),
permanently removing a Docker container (optionally its image), and updating *every*
container with an available image update in one shot. It only takes effect when
`UNRAID_MCP_ALLOW_MUTATIONS` is *also* true; enabling it alone unlocks nothing. These
tools are flagged destructive and still require `confirm=true`. See
[docs/security.md](docs/security.md) for the full tier breakdown.

Beyond tools, the server also exposes two MCP **resources** (`unraid://health` and
`unraid://system-info`) that a client can read without spending a tool call, and a
**`triage`** prompt that walks an agent through investigating the box. See
[Resources & prompts](#resources--prompts) below.

The full tool catalog and agent-side conventions live in
[docs/llm-usage.md](docs/llm-usage.md).

## Quick start

### Docker (easiest)

The image is on Docker Hub as
[`dtarakanov/unraid-mcp`](https://hub.docker.com/r/dtarakanov/unraid-mcp) (multi-arch).
It runs the streamable-HTTP transport on port 6750.

```bash
docker run --rm -p 127.0.0.1:6750:6750 \
  -e UNRAID_API_URL=https://yourhash.myunraid.net/graphql \
  -e UNRAID_API_KEY=your-api-key \
  -e UNRAID_MCP_BEARER_TOKEN=$(openssl rand -hex 32) \
  dtarakanov/unraid-mcp:latest
```

This publishes the port on localhost only. To reach it from other machines, drop the
`127.0.0.1:` prefix — but set `UNRAID_MCP_ALLOWED_HOSTS` and put TLS in front first
(see [docs/security.md](docs/security.md)).

Or grab [`docker-compose.yml`](docker-compose.yml), fill in the values, and
`docker compose up -d`.

`GET /health` is an unauthenticated liveness check — no bearer token, no call to
the Unraid API, just a static `200 {"status":"ok"}`. Point Docker `HEALTHCHECK`,
uptime monitors, or a reverse-proxy upstream check at it instead of embedding the
bearer token in monitoring config. (This is separate from the `unraid://health`
MCP resource below, which does summarize real array/disk/UPS state and requires
the bearer token like every other `/mcp` request.)

Use a URL whose hostname matches the Unraid certificate. The MyUnraid hostname
shown by Unraid usually works with `UNRAID_VERIFY_SSL=true`. If you use a LAN IP
URL and the cert does not include that IP, a CA bundle cannot fix hostname
validation; use a matching cert/hostname or set `UNRAID_VERIFY_SSL=false` only
on a trusted LAN.

### Local (for stdio clients)

```bash
git clone https://github.com/tarakanof/Unraid-MCP
cd Unraid-MCP
uv sync
uv run unraid-mcp        # stdio transport (the default)
```

(`python -m unraid_mcp` also works once the venv is activated.)

### On Unraid (bundled template)

To run it *on* Unraid, talking back to the same box, use the ready-made
Community-Apps template at [`deploy/unraid/unraid-mcp.xml`](deploy/unraid/unraid-mcp.xml).
Install it as a **user template**: copy that file into
`/boot/config/plugins/dockerMan/templates-user/` on the flash drive, then
**Docker → Add Container** and pick **unraid-mcp** from the *Template* dropdown —
every field is pre-filled. The full walkthrough — API key, bearer token, the TLS
setting, and a few ways to get the file onto the box — is in
[deploy/unraid/](deploy/unraid/).

## Configure

Two variables are required:

- `UNRAID_API_URL` — your GraphQL endpoint, e.g. `https://yourhash.myunraid.net/graphql`
- `UNRAID_API_KEY` — an Unraid API key (a `guest`/read key is plenty for monitoring)

Copy `.env.example` to `.env` for a starting point. Everything else — transports, TLS,
the safety switches — is in [docs/configuration.md](docs/configuration.md).

## Connect a client

Local stdio clients launch the server themselves:

```json
{
  "mcpServers": {
    "unraid": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/unraid-mcp", "unraid-mcp"],
      "env": {
        "UNRAID_API_URL": "https://yourhash.myunraid.net/graphql",
        "UNRAID_API_KEY": "your-api-key"
      }
    }
  }
}
```

Claude Desktop uses the same `mcpServers` shape. For remote/HTTP, point the client at
`http://<host>:6750/mcp` and send `Authorization: Bearer <token>`. Put the HTTP
transport behind TLS before exposing it beyond localhost or a trusted LAN.

Writing an agent against this? [docs/llm-usage.md](docs/llm-usage.md) has the tool
catalog, conventions, and a drop-in system-prompt snippet.

## Resources & prompts

The server exposes MCP **resources** and **prompts** in addition to tools. They add no
new API surface — each resource returns exactly the same JSON as the matching read tool,
and the prompt just orchestrates existing tools.

**Resources** (read-only, always available):

| URI | Same data as | Contents |
| --- | --- | --- |
| `unraid://health` | `get_health_summary` | Array state, capacity, unhealthy disks, parity status, UPS, unread notification counts |
| `unraid://system-info` | `get_system_info` | OS/kernel, CPU, memory, motherboard, versions, uptime, flash identity |

**Prompt** (always available):

| Name | Argument | What it does |
| --- | --- | --- |
| `triage` | `focus` (optional string) | Instructs the agent to start from `get_health_summary`, then drill into whichever subsystem (disks, notifications, parity, UPS, services) is unhealthy. `focus` narrows the investigation to a named subsystem. |

If the box is unreachable, a resource read fails with a clean, secret-free error rather
than crashing the client.

**How clients surface these:**

- **Claude Desktop** — open the chat's attachment (`+` / paperclip) menu and pick
  **Add from Unraid** to attach a resource (e.g. the health summary) into the
  conversation. Prompts appear in the same menu (sometimes shown as "commands" or
  slash-style entries); choose **triage**, fill in the optional `focus`, and it drops
  the triage instructions into your message.
- **Claude Code** — MCP resources are referenced with `@unraid:` mentions (type `@` and
  pick the server/resource), and MCP prompts are exposed as slash commands, e.g.
  `/unraid:triage` with an optional focus argument. Run `/mcp` to inspect what the
  `unraid` server offers.
- **Any MCP client / SDK** — list and read via the standard MCP calls:
  `resources/list` then `resources/read` with `uri: "unraid://health"`; `prompts/list`
  then `prompts/get` with `name: "triage"` and `arguments: {"focus": "disks"}`.

## Develop

```bash
uv sync --extra dev
uv run pytest        # all mocked — no live server needed
uv run ruff check .
uv run ruff format .
```

Tests mock the GraphQL endpoint with `respx`, so the suite runs without a real Unraid
server. The logic functions are kept pure and testable; the `@mcp.tool` wrappers are
thin.

The Docker image builds and pushes to Docker Hub automatically when a GitHub Release is
published — see [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml).

## Docs

- [docs/configuration.md](docs/configuration.md) — every environment variable, transports, TLS
- [docs/security.md](docs/security.md) — the security model
- [docs/llm-usage.md](docs/llm-usage.md) — tool catalog + guide for LLM agents
- [deploy/unraid/](deploy/unraid/) — running it on Unraid

## License

MIT — see [LICENSE](LICENSE).
