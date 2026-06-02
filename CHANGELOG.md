# Changelog

## 0.2.0 - Unreleased

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
