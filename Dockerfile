# Minimal image for running unraid-mcp over the streamable-HTTP transport.
# stdio clients usually launch the package directly via uv/python instead.
FROM python:3.12-slim

# Install uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app
USER app

# In a container the server must bind all interfaces; require a bearer token
# (set UNRAID_MCP_BEARER_TOKEN) and put TLS in front of it for remote use.
ENV UNRAID_MCP_TRANSPORT=streamable-http \
    UNRAID_MCP_HOST=0.0.0.0 \
    UNRAID_MCP_PORT=6750
EXPOSE 6750

# Liveness: the MCP port is accepting connections.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('UNRAID_MCP_PORT','6750'))), 3)" || exit 1

ENTRYPOINT ["unraid-mcp"]
