# Minimal image for running unraid-mcp over the streamable-HTTP transport.
# stdio clients usually launch the package directly via uv/python instead.
FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

# Install uv only for the build, then remove it from the runtime image.
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:78bc42400d77b0678ba95765305c826652ed5431f399257271dda681d0318f03 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Pick up Alpine security fixes newer than the pinned base digest, and drop
# pip (plus ensurepip's bundled wheels): the runtime never installs packages,
# and pip's vendored libraries otherwise show up in image vulnerability scans.
RUN apk upgrade --no-cache \
    && uv export --quiet --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && uv pip uninstall --system pip \
    && rm -rf requirements.txt /bin/uv /bin/uvx /usr/local/bin/pip* /usr/local/lib/python3.*/ensurepip

# Run as a non-root user.
RUN adduser -D -u 10001 app
USER app

# In a container the server must bind all interfaces; require a bearer token
# (set UNRAID_MCP_BEARER_TOKEN) and put TLS in front of it for remote use.
ENV UNRAID_MCP_TRANSPORT=streamable-http \
    UNRAID_MCP_HOST=0.0.0.0 \
    UNRAID_MCP_PORT=6750
EXPOSE 6750

# Liveness: GET /health returns 200 with no auth required and no upstream
# call, so the check never needs the bearer token or the Unraid API. The probe
# honors UNRAID_MCP_HOST/PORT and uses https (unverified, loopback) when TLS is on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "unraid_mcp.healthcheck"]

ENTRYPOINT ["unraid-mcp"]
