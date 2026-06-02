# Deploying unraid-mcp on Unraid

The server runs as a Docker container on Unraid using the **streamable-HTTP**
transport, and connects back to the same Unraid server's GraphQL API. MCP
clients then connect to `http://<TOWER-IP>:6750/mcp` with a bearer token.

> Image namespace: the template/compose use `tarakanof/unraid-mcp`. If your
> Docker Hub username differs, change the repository accordingly.

## Prerequisites

1. **Unraid 7.2+** (API built in) or the **Unraid Connect** plugin on older versions.
2. An **API key**: WebGUI → **Settings → Management Access → API Keys**
   (a `guest`/read role is enough for monitoring), or on the server terminal:
   ```bash
   unraid-api apikey --create --name mcp --roles guest
   ```
3. A **bearer token** for clients — generate a long random one:
   ```bash
   openssl rand -hex 32
   ```

## Option A — Add the template manually (no CA listing needed)

1. Copy [`unraid-mcp.xml`](./unraid-mcp.xml) to `/boot/config/plugins/dockerMan/templates-user/`
   on your Unraid server (the flash drive). The easiest way is the terminal:
   ```bash
   cd /boot/config/plugins/dockerMan/templates-user
   wget https://raw.githubusercontent.com/tarakanof/Unraid-MCP/main/deploy/unraid/unraid-mcp.xml
   ```
2. In the WebUI go to **Docker → Add Container**, and pick **unraid-mcp** from the
   *Template* dropdown (under "User templates").
3. Fill in:
   - **Unraid API URL** → `https://<TOWER-IP>/graphql` (your server's LAN IP)
   - **Unraid API Key** → the key from above
   - **MCP Bearer Token** → the random token from above
   - leave **Verify Unraid TLS** = `false` (local self-signed cert)
   - leave **Allow Mutations** = `false` unless you want write access
4. **Apply**. The container starts on port `6750`.

## Option B — Docker Compose / Portainer

Use [`../../docker-compose.yml`](../../docker-compose.yml) — fill in the env values and `docker compose up -d`.

## Verify

```bash
# From a host on your LAN — a bare GET returns HTTP, proving it's listening:
curl -i http://<TOWER-IP>:6750/mcp
# (A full MCP handshake is done by your MCP client, not curl.)
```
Check the container logs in the Unraid UI — on startup it logs
`Serving streamable-HTTP on http://0.0.0.0:6750/mcp`. If you left the bearer
token blank, the generated token is printed there (set a fixed one to avoid
this changing on restart).

## Connect a client

Point your MCP client at `http://<TOWER-IP>:6750/mcp` and send the header
`Authorization: Bearer <your-token>`. See the repo README and
[`docs/llm-usage.md`](../../docs/llm-usage.md) for client wiring and tool usage.

## Security notes

- **Read-only by default.** Mutations require `Allow Mutations = true` *and* a
  per-call `confirm=true`.
- The container binds `0.0.0.0` (required in Docker). The bearer token is the
  gate; for use beyond a trusted LAN put it behind a **TLS reverse proxy**
  (SWAG / Nginx Proxy Manager) or set `UNRAID_MCP_TLS_CERT` + `UNRAID_MCP_TLS_KEY`
  and mount the certs. Set **Allowed Hosts** to keep DNS-rebinding protection on.
- Use a least-privilege Unraid API key; never commit it.
