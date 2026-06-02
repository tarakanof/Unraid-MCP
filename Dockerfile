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

ENTRYPOINT ["unraid-mcp"]
