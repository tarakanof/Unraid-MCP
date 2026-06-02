# Configuration

Everything is configured through environment variables (or a `.env` file — copy
`.env.example` and fill it in). Only the first two are required.

## Required

| Variable | Purpose |
|----------|---------|
| `UNRAID_API_URL` | GraphQL endpoint, e.g. `https://yourhash.myunraid.net/graphql`. If you leave off the path, `/graphql` is appended. |
| `UNRAID_API_KEY` | API key, sent as the `x-api-key` header. A `guest`/read-scoped key is enough for the read-only tools. |

Create a key in the Unraid WebGUI (**Settings → Management Access → API Keys**) or
on the server:

```bash
unraid-api apikey --create --name mcp --roles guest
```

Use a URL whose hostname matches the Unraid API certificate. MyUnraid hostnames
usually validate with `UNRAID_VERIFY_SSL=true`. LAN IP URLs often do not match
the certificate; a CA bundle can make a self-signed cert trusted, but it cannot
fix a hostname/IP mismatch.

## Everything else

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNRAID_VERIFY_SSL` | `true` | Verify the Unraid TLS cert. See the self-signed note below. |
| `UNRAID_CA_BUNDLE` | – | Path to a CA bundle (PEM) to trust the Unraid certificate. |
| `UNRAID_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `UNRAID_MCP_HOST` | `127.0.0.1` | Bind address for the HTTP transport. |
| `UNRAID_MCP_PORT` | `6750` | Port for the HTTP transport. |
| `UNRAID_MCP_BEARER_TOKEN` | – | Bearer token required from HTTP clients. If set, it must be at least 32 random characters and cannot be a placeholder. Auto-generated and printed to stderr if unset (changes every restart, so set a fixed one). |
| `UNRAID_MCP_ALLOWED_HOSTS` | – | Comma-separated Host allow-list for DNS-rebinding protection (HTTP). Set this whenever you bind to a non-localhost address. |
| `UNRAID_MCP_ALLOWED_ORIGINS` | – | Comma-separated Origin allow-list for DNS-rebinding protection (HTTP). |
| `UNRAID_MCP_TLS_CERT` | – | TLS certificate (PEM) to serve the HTTP transport over HTTPS. Set with the key below. |
| `UNRAID_MCP_TLS_KEY` | – | TLS private key (PEM). Both cert + key together enable HTTPS directly. |
| `UNRAID_MCP_ALLOW_MUTATIONS` | `false` | Register the state-changing tools. Each still requires `confirm=true`. |
| `UNRAID_MCP_ALLOW_RAW_QUERY` | `false` | Register the read-only raw GraphQL passthrough tool. |
| `UNRAID_MCP_TIMEOUT` | `30` | HTTP timeout to the Unraid API (seconds). |
| `UNRAID_MCP_LOG_LEVEL` | `INFO` | Log level (logs go to stderr): `DEBUG`/`INFO`/`WARNING`/`ERROR`. |

## Transports

- **`stdio`** (default) — the client launches the server as a subprocess and talks
  to it over stdin/stdout. No network surface. This is what Claude Desktop and most
  local harnesses use.
- **`streamable-http`** — an HTTP server on `UNRAID_MCP_HOST:UNRAID_MCP_PORT`, for
  remote clients (and the Docker/Unraid deployment). Clients connect to
  `http://<host>:<port>/mcp` with `Authorization: Bearer <UNRAID_MCP_BEARER_TOKEN>`.
  See [security.md](security.md) for how this transport is gated.

## TLS to the Unraid API (self-signed certs)

Unraid local hosts may ship a self-signed certificate, or a certificate for a
MyUnraid hostname instead of the LAN IP. Verification succeeds only when the
certificate is trusted and the URL hostname matches the certificate. Options:

1. **Use the matching hostname** — prefer the MyUnraid hostname shown by Unraid,
   such as `https://yourhash.myunraid.net/graphql`.
2. **Trust a self-signed cert** — point `UNRAID_CA_BUNDLE` at the Unraid
   certificate or issuing CA (PEM). This keeps verification on, but it does not
   fix a hostname/IP mismatch.
3. **Install a matching cert** — use a certificate whose subject alternative
   names include the hostname or IP in `UNRAID_API_URL`.
4. **Disable verification** — set `UNRAID_VERIFY_SSL=false` only on a trusted LAN.
   The server logs a warning when you do this.

Outbound requests to the Unraid API ignore ambient proxy environment variables
such as `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`, so the API path is not
silently routed through a process-level proxy. Configure direct network routing
to the Unraid API endpoint.
