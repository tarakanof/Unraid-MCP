# Changelog

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
