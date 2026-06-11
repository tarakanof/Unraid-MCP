# Minimal image for running unraid-mcp over the streamable-HTTP transport.
# stdio clients usually launch the package directly via uv/python instead.
FROM python:3.12-alpine@sha256:c93e680d8a99a9a36cd1667fc7267788e3dcebccdb4c4621b040c367a6f07fb6

# Install uv only for the build, then remove it from the runtime image.
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:78bc42400d77b0678ba95765305c826652ed5431f399257271dda681d0318f03 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv export --quiet --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm -f requirements.txt /bin/uv /bin/uvx

# Run as a non-root user.
RUN adduser -D -u 10001 app
USER app

# In a container the server must bind all interfaces; require a bearer token
# (set UNRAID_MCP_BEARER_TOKEN) and put TLS in front of it for remote use.
ENV UNRAID_MCP_TRANSPORT=streamable-http \
    UNRAID_MCP_HOST=0.0.0.0 \
    UNRAID_MCP_PORT=6750
EXPOSE 6750

# Liveness: the MCP port is accepting connections. Probe the configured bind
# address (wildcard binds are reachable via loopback).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; h=os.environ.get('UNRAID_MCP_HOST','0.0.0.0'); socket.create_connection(('127.0.0.1' if h in ('0.0.0.0','::') else h, int(os.environ.get('UNRAID_MCP_PORT','6750'))), 3)" || exit 1

ENTRYPOINT ["unraid-mcp"]
