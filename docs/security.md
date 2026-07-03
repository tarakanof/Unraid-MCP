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

## Tool output is untrusted data

Tool results echo strings that originate on the Unraid box — container names, share
comments, notification titles/descriptions, and **Docker container logs**. A hostile
or compromised service there could plant prompt-injection text in them — log output
is workload-controlled and gets special mention because it's often long, freeform,
and easy to overlook as "just output". MCP clients and agents should treat all tool
output as data, never as instructions.

Container logs can also contain secrets that the user's own containers print (API
keys, tokens, connection strings). That's inherent to reading logs and not something
this server can filter — treat `get_docker_container_logs` output with the same care
as any other secret-bearing log stream.

## No arbitrary execution

Only typed GraphQL operations are issued. The optional raw-query tool
(`UNRAID_MCP_ALLOW_RAW_QUERY=true`) parses the document and allows **only** `query`
operations — mutations and subscriptions are rejected, including ones hidden behind
comments or leading whitespace.
