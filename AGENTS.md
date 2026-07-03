# AGENTS.md — conventions for coding agents

MCP server exposing the Unraid GraphQL API as typed tools. Python 3.11+,
FastMCP (`mcp`), httpx, pydantic-settings. Source in `src/unraid_mcp/`,
tests in `tests/`.

## Commands

```sh
uv sync --extra dev                                 # install
uv run pytest -q                                    # test
uv run ruff check . && uv run ruff format --check . # lint + format
```

All three must pass before opening a PR.

Schema-drift check (network, run manually or via the weekly `schema-drift`
workflow) — validates every operation in `queries.py` against the upstream
`unraid/api` SDL:

```sh
uv run python scripts/check_schema_drift.py         # PASS/FAIL per op, non-zero on drift
```

## Architecture

- **Tool modules** live in `src/unraid_mcp/tools/`. Each exposes
  `register(mcp, settings)` for read tools (always on) and optionally
  `register_mutations(mcp, settings)` (registered only when
  `UNRAID_MCP_ALLOW_MUTATIONS=true`). See `tools/docker.py` for the pattern.
- **Logic is decoupled from MCP**: plain `fetch_*` / `do_*` async functions
  take an `UnraidClient` first argument so they are unit-testable without a
  running server. The `@mcp.tool` wrappers only call `_base.guarded(...)`.
- **GraphQL strings** live only in `queries.py`. Validate every field name
  against the upstream schema: https://github.com/unraid/api →
  `api/generated-schema.graphql`.
- **Response shaping** is pure functions in `formatting.py` (no I/O).
  Size conventions: `ArrayDisk`/`Share` sizes arrive in **KiB**, physical
  `Disk.size` arrives in **bytes**; every size is emitted as
  `{"bytes": int|None, "human": str|None}`.
- **Errors**: raise `ToolError` with an actionable, secret-free message.
  Client/domain errors are translated at the tool boundary by `_base.guarded`.

## Safety invariants (do not weaken)

- Mutating tools get a `MUTATING` or `DESTRUCTIVE` annotation and must call
  `_base.require_confirm(confirm, "<exact consequence>")` **before any
  network I/O**.
- The API key and bearer token must never appear in logs, error messages, or
  tool output (`SecretStr` + redaction filters in `logging.py`).
- Logs go to **stderr only** — stdout carries the stdio JSON-RPC stream.

## Tests

respx-based (`tests/test_tools_read.py`, `tests/test_tools_mutations.py`).
Every tool needs: happy path, empty/None-field response, error mapping.
Mutations additionally: refused without `confirm=true` **with no HTTP
request made**.

## When adding configuration

Update `.env.example`, `docker-compose.yml`, `docs/configuration.md`, and
`README.md` together with the code.

## Git / PR

- Conventional Commits (`fix(auth): ...`); no `Co-Authored-By` trailers.
- One issue per PR; squash merge; fill the PR template, including the live
  verification section, and tick the linked issue's acceptance criteria.
