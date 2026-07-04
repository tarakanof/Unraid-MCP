# Minimal image for running unraid-mcp over the streamable-HTTP transport.
# stdio clients usually launch the package directly via uv/python instead.
FROM python:3.14-alpine@sha256:26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92

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

# Liveness: GET /health returns 200 with no auth required and no upstream
# call, so the check never needs the bearer token or the Unraid API.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request as u; h=os.environ.get('UNRAID_MCP_HOST','0.0.0.0'); h='127.0.0.1' if h in ('0.0.0.0','::') else h; p=os.environ.get('UNRAID_MCP_PORT','6750'); u.urlopen(f'http://{h}:{p}/health', timeout=3)" || exit 1

ENTRYPOINT ["unraid-mcp"]
