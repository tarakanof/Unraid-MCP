# Deploying unraid-mcp on Unraid

The server runs as a Docker container on Unraid using the **streamable-HTTP**
transport, and connects back to the same Unraid server's GraphQL API. MCP
clients then connect to `http://<TOWER-IP>:6750/mcp` with a bearer token.

> Image namespace: the template/compose use `dtarakanov/unraid-mcp`. If your
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

1. Copy `unraid-mcp.xml` into `/boot/config/plugins/dockerMan/templates-user/` on the
   Unraid flash drive. Easiest — straight from the Unraid web terminal:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/unraid-mcp.xml \
     https://raw.githubusercontent.com/tarakanof/Unraid-MCP/main/deploy/unraid/unraid-mcp.xml
   ```
   Or copy it from a machine that has the repo cloned — any of:
   - **Finder / SMB:** open `smb://<TOWER-IP>` → the `flash` share →
     `config/plugins/dockerMan/templates-user/` and drag `unraid-mcp.xml` in.
   - **scp:** `scp deploy/unraid/unraid-mcp.xml root@<TOWER-IP>:/boot/config/plugins/dockerMan/templates-user/`
2. In the WebUI go to **Docker → Add Container**, and pick **unraid-mcp** from the
   *Template* dropdown (under "User templates").
3. Fill in:
   - **Unraid API URL** → `https://<MYUNRAID-HOST>/graphql` (the hostname that
     matches the Unraid certificate)
   - **Unraid API Key** → the key from above
   - **MCP Bearer Token** → the random token from above
   - leave **Verify Unraid TLS** = `true` when the cert is trusted and matches
     the URL hostname; if you use `https://<TOWER-IP>/graphql`, verification
     usually fails unless the cert includes that IP
   - for a self-signed cert with a matching hostname, mount a CA bundle and set
     `UNRAID_CA_BUNDLE`; a CA bundle does not fix hostname/IP mismatches
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
`Serving streamable-HTTP on http://0.0.0.0:6750/mcp`.

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
- Weak placeholder bearer tokens are rejected at startup; generate one with
  `openssl rand -hex 32`.
- Use a least-privilege Unraid API key; never commit it.

## Visibility

- The GitHub repo and the Docker Hub image (`dtarakanov/unraid-mcp`) are both **public**,
  so the template icon renders and Unraid pulls the image with no credentials.
- It isn't listed in **Community Applications** yet — install it as a user template
  (Option A) for now. Getting it into the Apps store is a separate step (the public repo
  is the prerequisite, plus a forum support thread and CA moderation).
