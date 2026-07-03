# Linked issue

Closes #

## Summary

<!-- What changed and why, in 2-4 sentences. One issue per PR. -->

## Acceptance criteria

<!-- Copy the checklist from the linked issue. Tick each box and note the
     evidence (test name, file:line, or PR comment) next to it. -->

- [ ]

## Tests

- [ ] `uv run pytest -q` green; `uv run ruff check .` and `uv run ruff format --check .` clean
- [ ] New/changed behavior is covered by tests (list them below)
- [ ] Mutations only: a test proves the tool refuses without `confirm=true` **and makes no HTTP request**

<!-- List the new/updated test functions here. -->

## Live verification

<!-- Run the linked issue's "Verification" section against a real Unraid
     server and paste the relevant output here, or explain why it is not
     applicable (e.g. pure refactor, CI-only change). -->

## Security

- [ ] No secrets (API key, bearer token) can reach logs, error messages, or tool output
- [ ] `docs/security.md` updated if the security surface changed (new tool tier, new untrusted data source, new config)

## Docs

- [ ] `README.md`, `.env.example`, `docker-compose.yml`, and `docs/` updated if tools or configuration changed
