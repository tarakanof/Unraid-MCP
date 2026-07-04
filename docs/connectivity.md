# Connectivity guide

How to reach the MCP server from a client, per network topology — from a local
stdio subprocess to a TLS-terminating reverse proxy. Each recipe is copy-paste
where it can be; the two things that never change are called out first.

## Two rules that hold in every variant

1. **The bearer token is mandatory. Always.** No recipe here disables it. A
   network layer like Tailscale or WireGuard authenticates the *device* that
   reached the socket — it says nothing about *which process* on that device is
   talking, and it does nothing on a shared or compromised host. The bearer
   token is the only thing that authenticates the MCP client itself, so it
   stays on top of whatever transport you pick. Generate one with
   `openssl rand -hex 32`.

2. **A non-localhost bind needs `UNRAID_MCP_ALLOWED_HOSTS`.** The moment you
   bind anything other than loopback, set the Host allow-list — otherwise
   DNS-rebinding protection turns off and only the bearer token guards access.
   The exact value depends on the recipe; the matching rules are spelled out in
   [How the Host allow-list matches](#how-the-host-allow-list-matches) below,
   and every recipe gives you the exact string to use.

## Pick a variant

| Variant | Pick it when | Auth layers | Effort |
|---------|--------------|-------------|--------|
| **[stdio](#stdio-local)** | The client runs on the same machine and can launch a subprocess (Claude Desktop, local Claude Code). | OS process boundary → *(no bearer needed; no network)* | Trivial |
| **[LAN HTTP](#lan-http)** | Client and server share a trusted home LAN; you accept a plaintext bearer token on the wire. | LAN + **bearer** + Host allow-list | Low |
| **[Tailscale](#tailscale-recommended-for-remote)** | Remote access without opening a port. Best default for "reach it from anywhere". | Tailnet (WireGuard, device-authed) + **bearer** + real `*.ts.net` TLS | Low–medium |
| **[WireGuard](#wireguard-unraid-built-in-vpn)** | You already run Unraid's built-in VPN; collapses to the LAN recipe once connected. | WireGuard tunnel + **bearer** + Host allow-list | Low (VPN already up) |
| **[SSH tunnel](#ssh-tunnel)** | One-off / occasional remote access from a box you can SSH to. | SSH + **bearer** | Low, per-session |
| **[SWAG](#swag)** | You already run SWAG and want a stable `https://name.example.com` with a real cert. | Reverse proxy (TLS, optional Authelia/Authentik) + **bearer** + Host allow-list | Medium |
| **[Nginx Proxy Manager](#nginx-proxy-manager-untested)** | Same as SWAG but you run NPM instead. | Reverse proxy (TLS) + **bearer** + Host allow-list | Medium |
| **[Caddy / Traefik](#other-reverse-proxies-caddy--traefik)** | Any other reverse proxy — generic header/SSE rules. | Reverse proxy (TLS) + **bearer** + Host allow-list | Medium |
| **[Public internet](#not-recommended-cloudflare-tunnel--public-internet-exposure)** | — | **Not recommended.** See below. | — |

Standard proxy shape for every reverse-proxy recipe: **subdomain only**
(`mcp.example.com` → container port `6750`, MCP path `/mcp`), the proxy
**preserves the original `Host` header**, and you add the public name to
`UNRAID_MCP_ALLOWED_HOSTS`. Subpath hosting (`example.com/mcp/...`) is possible
with a path rewrite but is not covered here.

## How the Host allow-list matches

`UNRAID_MCP_ALLOWED_HOSTS` drives DNS-rebinding protection. Getting the exact
string right is the one thing people trip on, so here is precisely what the code
does (`config.py::http_allowed_hosts` + the MCP SDK's `TransportSecurityMiddleware`):

- The incoming request's **`Host` header is compared by exact string match**
  against the allow-list. No suffix logic, no domain matching — the whole
  `Host` value must appear verbatim in the list.
- The server **always adds three entries for you**: `<bind-host>:<port>`,
  `127.0.0.1:<port>`, and `localhost:<port>` (using `UNRAID_MCP_HOST` /
  `UNRAID_MCP_PORT`). Your `UNRAID_MCP_ALLOWED_HOSTS` entries are added on top,
  verbatim.
- **The port is part of the match, and clients only put a port in the `Host`
  header when it is non-default for the scheme.** A client hitting
  `https://mcp.example.com/mcp` (port 443) sends `Host: mcp.example.com` — **no
  port** — so the allow-list needs `mcp.example.com`. A client hitting
  `http://tower.local:6750/mcp` sends `Host: tower.local:6750` — **with port** —
  so the allow-list needs `tower.local:6750`. This is why proxy recipes (TLS on
  443) list a bare name and the LAN recipe lists `name:6750`.
- A wildcard-port entry `name:*` matches that name on **any** port (e.g.
  `tower.local:*`). Handy if clients reach the box under one name but varying
  ports; otherwise prefer the exact form.
- **`Origin` is separate and rarely matters here.** The SDK only rejects a
  request on `Origin` if the header is *present* and unlisted; non-browser MCP
  clients don't send `Origin`, so they pass. Set `UNRAID_MCP_ALLOWED_ORIGINS`
  only if a browser-based client connects. This guide's recipes don't need it.

A missing entry shows up as HTTP **421 "Invalid Host header"** (or 403 for a
bad `Origin`). If a client that connected fine suddenly 421s, the `Host` it
sends isn't in the list — add the exact value.

---

## stdio (local)

The simplest and safest: the client launches the server as a subprocess and
talks over stdin/stdout. No network surface, no bearer token, no ports. This is
what Claude Desktop and most local harnesses use.

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

The client and the server run on the same machine; the only thing crossing a
network is the outbound HTTPS call to the Unraid API. Nothing else to configure.

## LAN HTTP

Run the streamable-HTTP transport and reach it from other machines on a trusted
home LAN. The bearer token crosses the LAN **unencrypted** — acceptable on a
network you trust, not otherwise. If that's not acceptable, put TLS in front
(a reverse-proxy recipe) or tunnel it (Tailscale / WireGuard / SSH).

```bash
docker run --rm -p 6750:6750 \
  -e UNRAID_API_URL=https://yourhash.myunraid.net/graphql \
  -e UNRAID_API_KEY=your-api-key \
  -e UNRAID_MCP_BEARER_TOKEN=$(openssl rand -hex 32) \
  -e UNRAID_MCP_HOST=0.0.0.0 \
  -e UNRAID_MCP_ALLOWED_HOSTS=tower.local:6750,192.168.1.10:6750 \
  dtarakanov/unraid-mcp:latest
```

- `UNRAID_MCP_HOST=0.0.0.0` binds all interfaces (inside a container you must —
  the published port maps to it). Dropping the `127.0.0.1:` from `-p` publishes
  it on the LAN.
- `UNRAID_MCP_ALLOWED_HOSTS` lists **every name/IP a client actually connects
  with, including `:6750`** (see [the matching rules](#how-the-host-allow-list-matches)).
  Add each one — `tower.local:6750`, the LAN IP, etc.
- Point clients at `http://tower.local:6750/mcp` with
  `Authorization: Bearer <token>`.

The server logs a plaintext-HTTP warning on a non-localhost bind — expected
here, and the reason this recipe is LAN-only. See [issue #50] for hardening a
deployed instance (the ALLOWED_HOSTS / TLS decisions this guide documents).

## Tailscale (recommended for remote)

Reach the box from anywhere without publishing a port or terminating TLS
yourself. Tailscale's `serve` gives you a **real, publicly-trusted `*.ts.net`
certificate** and keeps the listener tailnet-only — off the public internet
entirely.

**Primary recipe — per-container Tailscale (Unraid 6.12.9+ / 7.x).** Recent
Unraid builds can attach a Tailscale sidecar to a single container from the
Docker template, so the MCP container gets its own `*.ts.net` name without a
host-level plugin:

1. Edit the `unraid-mcp` container in the Unraid Docker tab and toggle
   **"Use Tailscale"** on. Authenticate it to your tailnet when prompted (it
   appears as its own device, e.g. `unraid-mcp`).
2. Keep the MCP container itself **unpublished** — no `-p` / port mapping to the
   LAN. Tailscale reaches it inside the container network. Bind the server to
   `0.0.0.0` so the Tailscale sidecar can connect to it, and keep the bearer
   token set.
3. In the container's **Tailscale → Serve** settings, expose the MCP port over
   HTTPS: serve `https / → http://127.0.0.1:6750`. Tailscale terminates TLS with
   a real `*.ts.net` cert and forwards to the server. (Use **Serve**, not
   **Funnel** — Funnel publishes to the public internet, which this guide
   [recommends against](#not-recommended-cloudflare-tunnel--public-internet-exposure).)
4. Set the allow-list to the tailnet name. Serve terminates on 443, so the
   `Host` header is the bare name with **no port**:

   ```
   UNRAID_MCP_ALLOWED_HOSTS=unraid-mcp.<your-tailnet>.ts.net
   ```

5. From any tailnet device, point the client at
   `https://unraid-mcp.<your-tailnet>.ts.net/mcp` with
   `Authorization: Bearer <token>`. Real cert, so `UNRAID_VERIFY_SSL`-style
   client TLS just works — no cert wrangling.

**Fallback — host-level Tailscale plugin.** On builds without the per-container
toggle, run the Unraid Community Apps **Tailscale** plugin on the host and
`tailscale serve https / http://127.0.0.1:6750` there (publish the MCP port on
`127.0.0.1` only so nothing but Tailscale can reach it). The client URL is then
`https://<tower>.<your-tailnet>.ts.net/mcp`, and `UNRAID_MCP_ALLOWED_HOSTS` is
that same bare host name. Same shape, host-wide instead of per-container.

## WireGuard (Unraid built-in VPN)

Unraid ships WireGuard under **Settings → VPN Manager**. Bring up a tunnel
(remote-access config), connect your client to it, and the MCP box is reachable
at its LAN address over an encrypted tunnel. At that point this **reduces to the
[LAN HTTP](#lan-http) recipe** — same `0.0.0.0` bind, same
`UNRAID_MCP_ALLOWED_HOSTS=tower.local:6750,<lan-ip>:6750`, same client URL over
`http://…:6750/mcp` — except the traffic now rides the WireGuard tunnel instead
of the bare LAN, so the plaintext-bearer concern is covered by the tunnel's
encryption. Tailscale (above) is the lower-friction option if you don't already
run WireGuard; WireGuard wins if the tunnel is already part of your setup.

## SSH tunnel

For occasional remote access from a machine you can SSH into the box (or its
network) from, forward the port over SSH and treat it as local:

```bash
ssh -N -L 6750:127.0.0.1:6750 root@tower.local
```

Then point the client at `http://127.0.0.1:6750/mcp` with the bearer token —
the `Host` is `127.0.0.1:6750`, which is always in the allow-list, so no
`UNRAID_MCP_ALLOWED_HOSTS` change is needed. Publish the MCP port on the box's
`127.0.0.1` only. The tunnel encrypts the bearer token in transit. Per-session
and manual, so it's a stopgap rather than a standing setup — use Tailscale or
WireGuard for anything recurring.

## SWAG

[SWAG](https://docs.linuxserver.io/general/swag/) (nginx + Let's Encrypt) gives
you a stable `https://unraid-mcp.example.com` with a real certificate. This repo
ships a ready drop-in: **[`deploy/swag/unraid-mcp.subdomain.conf`](../deploy/swag/unraid-mcp.subdomain.conf)**.

1. **DNS** — add a CNAME for `unraid-mcp` pointing at your domain (or an A
   record), so `unraid-mcp.example.com` resolves to SWAG.
2. **Install the conf** — copy the drop-in into SWAG's proxy-confs and restart:

   ```bash
   cp deploy/swag/unraid-mcp.subdomain.conf \
      /mnt/user/appdata/swag/nginx/proxy-confs/
   docker restart swag
   ```

   The file follows the linuxserver `*.subdomain.conf` convention
   (`server_name unraid-mcp.*`, `$upstream_*` variables, `resolver.conf`
   include) and adds the two things nginx gets wrong for MCP out of the box:
   **`proxy_buffering off`** (streamable-HTTP responses are SSE streams; the
   default buffering stalls them) and an HTTP/1.1 upstream with the standard
   `Connection`/`Upgrade` headers (via `proxy.conf`) plus long read timeouts for
   the long-lived stream.
3. **Upstream target** — the conf sets `$upstream_app unraid-mcp`. That name
   resolves only if SWAG and the MCP container share a user-defined Docker
   network; otherwise edit it to the Unraid host's LAN IP (e.g. `192.168.1.10`).
4. **Allow-list** — SWAG's `proxy.conf` preserves the original `Host`
   (`proxy_set_header Host $host`), so the server sees `unraid-mcp.example.com`.
   Add that exact name (no port — TLS terminates on 443) to the MCP container:

   ```
   UNRAID_MCP_ALLOWED_HOSTS=unraid-mcp.example.com
   ```

5. **Connect** — `https://unraid-mcp.example.com/mcp` with
   `Authorization: Bearer <token>`. The unauthenticated liveness probe is at
   `https://unraid-mcp.example.com/health`.

Want `mcp.example.com` instead of `unraid-mcp.example.com`? Rename the file to
`mcp.subdomain.conf`, change `server_name mcp.*;`, and set
`UNRAID_MCP_ALLOWED_HOSTS=mcp.example.com`. For an extra auth layer in front of
the bearer token, uncomment the Authelia/Authentik or `auth_basic` blocks in the
conf — but the bearer token stays regardless.

## Nginx Proxy Manager (untested)

> **Untested — written from the docs, not verified end-to-end. Reports
> welcome** (open an issue with what worked or didn't). The SWAG and Tailscale
> recipes above are the live-verified paths.

In NPM, add a **Proxy Host**:

- **Domain Names**: `mcp.example.com`
- **Scheme**: `http`, **Forward Hostname/IP**: the MCP container or the Unraid
  LAN IP, **Forward Port**: `6750`
- **SSL** tab: request a Let's Encrypt cert, enable **Force SSL** and **HTTP/2**.
- **Websockets Support**: **on**. NPM buffers proxied responses by default;
  the websockets toggle switches the location to an HTTP/1.1 upgrade-capable
  proxy, which is what lets the SSE stream flow. If streaming still stalls, add
  to the **Advanced** tab:

  ```nginx
  proxy_buffering off;
  proxy_http_version 1.1;
  proxy_read_timeout 3600s;
  ```

- NPM forwards the original `Host` by default, so set
  `UNRAID_MCP_ALLOWED_HOSTS=mcp.example.com` (bare name, TLS on 443).
- Connect at `https://mcp.example.com/mcp` with the bearer token.

## Other reverse proxies (Caddy / Traefik)

Any reverse proxy works if it does three things:

1. **Preserve the original `Host` header** and forward it upstream (Caddy's
   `reverse_proxy` does this by default; Traefik passes it through). Then set
   `UNRAID_MCP_ALLOWED_HOSTS` to that public name — bare name if TLS terminates
   on 443, `name:port` otherwise.
2. **Don't buffer the response.** Streamable-HTTP replies are SSE streams;
   response buffering stalls them until the stream closes. Caddy doesn't buffer
   by default. Traefik doesn't buffer unless a `buffering` middleware is
   attached — don't attach one to this route.
3. **Use HTTP/1.1 upstream with a long read timeout** so the long-lived stream
   isn't cut off mid-flight.

Minimal Caddy example:

```caddy
mcp.example.com {
    reverse_proxy 192.168.1.10:6750
}
```

Caddy fetches its own cert, preserves `Host`, and streams without buffering —
so `UNRAID_MCP_ALLOWED_HOSTS=mcp.example.com` and you're done.

## NOT recommended: Cloudflare Tunnel / public-internet exposure

**Don't put this server on the public internet** — not via Cloudflare Tunnel,
not via a port-forward, not via Funnel. This is a deliberate anti-recommendation,
so here's the reasoning rather than just "don't":

- **A bearer token is not sufficient auth for an internet-facing service that
  controls a NAS**, even read-only. It's a single shared secret with no
  rotation, no rate limiting, no lockout, no per-client identity, no MFA. On the
  open internet it faces continuous automated probing; one leak (a log, a
  screenshot, a client-config sync) is total compromise.
- **Read-only is still a real exfiltration surface.** The read tools expose
  system info, hardware identifiers (`get_system_info` returns the flash GUID —
  your license identifier), share layouts, container and system **logs** (which
  routinely contain secrets your own containers print), and more. That's a lot
  to hand an anonymous internet caller who guesses or steals one token.
- **Cloudflare Tunnel specifically** removes the "at least it's not routable"
  backstop: it makes the box reachable from anywhere by design, and layering
  Cloudflare Access (SSO/MFA) in front only moves the trust boundary to
  Cloudflare — it doesn't change that a bearer token alone behind it is weak.

If you need remote access, use **[Tailscale](#tailscale-recommended-for-remote)**
or **[WireGuard](#wireguard-unraid-built-in-vpn)**: the box stays off the public
internet, the network layer authenticates the device with real keys, and the
bearer token still guards the client on top. That's the same "reach it from
anywhere" outcome without the exposure.

## Monitoring and health checks

The streamable-HTTP transport serves an **unauthenticated `GET /health`**
liveness endpoint (added in [#70]) — a static `200 {"status":"ok"}` that never
calls the Unraid API and never touches the bearer gate. Point uptime monitors,
Docker `HEALTHCHECK`, and reverse-proxy upstream checks at `/health` so **the
bearer token never has to land in monitoring config**:

- **Docker / compose** — [`docker-compose.yml`](../docker-compose.yml) already
  ships the pattern (a `python -c urllib` probe against `/health`, since the
  image has no curl/wget). The image also bakes in a `HEALTHCHECK`, so Unraid's
  Docker tab shows health without any extra config.
- **Uptime monitors** (Uptime Kuma, etc.) — monitor
  `http(s)://<your-mcp-host>/health` and expect `200`. Behind a reverse proxy
  that's `https://mcp.example.com/health`; the proxy recipes above pass `/health`
  through alongside `/mcp`.
- **Reverse-proxy upstream health** — SWAG/NPM/Caddy can probe `/health` on the
  upstream to mark it up/down. It requires no auth, so no secret in the proxy
  config.

`/health` reports only liveness — it does **not** confirm the box is reachable.
For real array/disk/UPS state, use the `unraid://health` MCP resource or the
`get_health_summary` tool, both of which require the bearer token like every
other `/mcp` request.

## See also

- [docs/security.md](security.md) — the full security model (bearer gate, Host
  validation, TLS, why `/health` is a safe unauthenticated exception).
- [docs/configuration.md](configuration.md) — every environment variable
  referenced here.
- [issue #50] — hardening a deployed instance; this guide documents the
  `ALLOWED_HOSTS` / TLS decisions that issue acts on.

[issue #50]: https://github.com/tarakanof/Unraid-MCP/issues/50
[#70]: https://github.com/tarakanof/Unraid-MCP/pull/70
