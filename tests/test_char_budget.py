"""Total character budget for log and raw-query results (#159)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from mcp.client import Client

from tests.conftest import URL, make_settings
from unraid_mcp.formatting import MAX_LOG_RESULT_CHARS, MAX_RAW_RESULT_CHARS
from unraid_mcp.server import build_server
from unraid_mcp.tools import docker, misc


def _ser(value):
    return len(json.dumps(value, ensure_ascii=False))


def _log_file_payload(content, start=1, total=10_000):
    return {
        "logFile": {
            "path": "/var/log/syslog",
            "content": content,
            "totalLines": total,
            "startLine": start,
        }
    }


async def test_container_logs_quote_heavy_serialized_within_budget(mocked_client):
    payload = _logs_payload(1000, 1990)
    for line in payload["docker"]["logs"]["lines"]:
        line["message"] = '"\\' * 995
    async with mocked_client(_resp(payload)) as (c, _r):
        out = await docker.fetch_container_logs(c, "1:abcdef", tail=1000)
    assert _ser(out) <= MAX_LOG_RESULT_CHARS
    assert out["truncated"] is True
    assert "cannot be retrieved" in out["hint"]


async def test_read_log_file_quote_heavy_serialized_within_budget(mocked_client):
    content = "\n".join('"\\' * 1000 for _ in range(500)) + "\n"
    async with mocked_client(_resp(_log_file_payload(content))) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog", lines=500)
    assert _ser(out) <= MAX_LOG_RESULT_CHARS
    assert out["truncated"] is True
    assert out["next_start_line"] == 1 + out["content"].count("\n")


async def test_read_log_file_crlf_and_formfeed_count_only_newlines(mocked_client):
    content = "".join(f"{i:03d}\x0c" + "z" * 1000 + "\r\n" for i in range(100))
    async with mocked_client(_resp(_log_file_payload(content, start=5))) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog", lines=100, start_line=5)
    assert _ser(out) <= MAX_LOG_RESULT_CHARS
    kept = out["content"].count("\n")
    assert 0 < kept < 100
    assert out["next_start_line"] == 5 + kept
    assert out["omitted_lines"] == 100 - kept
    assert out["content"].endswith("\r\n")


async def test_read_log_file_exact_boundary(mocked_client):
    from unraid_mcp.formatting import _RESULT_RESERVE_CHARS

    avail = MAX_LOG_RESULT_CHARS - _RESULT_RESERVE_CHARS - len(json.dumps("/var/log/syslog"))
    fits = "a" * (avail - 2) + "\n"  # escaped length == avail
    async with mocked_client(_resp(_log_file_payload(fits))) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog")
    assert "truncated" not in out
    async with mocked_client(_resp(_log_file_payload(fits + "b"))) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog")
    assert out["truncated"] is True
    assert out["content"] == fits
    assert _ser(out) <= MAX_LOG_RESULT_CHARS


async def test_read_log_file_single_oversized_line(mocked_client):
    content = ('"' * 100_000) + "\nnext\n"
    async with mocked_client(_resp(_log_file_payload(content, start=7))) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog", start_line=7)
    assert _ser(out) <= MAX_LOG_RESULT_CHARS
    assert out["line_truncated"] is True
    assert out["content"].endswith("… [truncated]")
    assert out["next_start_line"] == 8
    assert out["omitted_lines"] == 1


async def test_read_log_file_missing_start_line_falls_back(mocked_client):
    content = "".join("y" * 999 + "\n" for _ in range(100))
    payload = _log_file_payload(content)
    payload["logFile"]["startLine"] = None
    async with mocked_client(_resp(payload)) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog", lines=100)
    assert out["next_start_line"] == 1 + out["content"].count("\n")


async def test_read_log_file_missing_start_line_uses_requested(mocked_client):
    content = "".join("y" * 999 + "\n" for _ in range(100))
    payload = _log_file_payload(content)
    payload["logFile"]["startLine"] = None
    async with mocked_client(_resp(payload)) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog", lines=100, start_line=500)
    assert out["next_start_line"] == 500 + out["content"].count("\n")


async def test_raw_query_quote_heavy_envelope_within_budget(mocked_client):
    big = {"a": ['"\\' * 50_000]}
    async with mocked_client(_resp(big)) as (client, _r):
        out = await misc.do_raw_query(client, "query { a }")
    assert out["truncated"] is True
    assert _ser(out) <= MAX_RAW_RESULT_CHARS


def _resp(data):
    return httpx.Response(200, json={"data": data})


def _logs_payload(n, size, cursor="c"):
    return {
        "docker": {
            "logs": {
                "containerId": "1:abcdef",
                "lines": [
                    {"timestamp": "2024-01-01T00:00:00Z", "message": f"{i:04d}" + "x" * size}
                    for i in range(n)
                ],
                "cursor": cursor,
            }
        }
    }


async def test_container_logs_char_budget_keeps_newest(mocked_client):
    async with mocked_client(_resp(_logs_payload(1000, 3000))) as (c, _r):
        out = await docker.fetch_container_logs(c, "1:abcdef", tail=1000)
    assert _ser(out) <= MAX_LOG_RESULT_CHARS
    assert out["truncated"] is True
    assert out["truncation_reason"] == "char_budget"
    assert out["omitted_lines"] + len(out["lines"]) == 1000
    assert out["lines"][-1]["message"].startswith("0999")
    assert out["cursor"] == "c"


async def test_container_logs_small_unaffected(mocked_client):
    async with mocked_client(_resp(_logs_payload(3, 10))) as (c, _r):
        out = await docker.fetch_container_logs(c, "1:abcdef")
    assert set(out) == {"container_id", "lines", "cursor", "truncated"}
    assert out["truncated"] is False


async def test_read_log_file_char_budget_next_start_line(mocked_client):
    content = "\n".join(f"{i:03d}" + "y" * 1999 for i in range(500)) + "\n"
    payload = {
        "logFile": {
            "path": "/var/log/syslog",
            "content": content,
            "totalLines": 900,
            "startLine": 10,
        }
    }
    async with mocked_client(_resp(payload)) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog", lines=500, start_line=10)
    assert _ser(out) <= MAX_LOG_RESULT_CHARS
    assert out["truncated"] is True
    assert out["truncation_reason"] == "char_budget"
    kept = out["content"].count("\n")
    assert out["next_start_line"] == 10 + kept
    assert content.splitlines()[kept].startswith(f"{kept:03d}")  # first omitted line
    assert out["omitted_lines"] == 500 - kept


async def test_read_log_file_small_unaffected(mocked_client):
    payload = {
        "logFile": {"path": "/var/log/syslog", "content": "a\nb\n", "totalLines": 2, "startLine": 1}
    }
    async with mocked_client(_resp(payload)) as (c, _r):
        out = await misc.fetch_log_file(c, "/var/log/syslog")
    assert out == {
        "path": "/var/log/syslog",
        "content": "a\nb\n",
        "total_lines": 2,
        "start_line": 1,
    }


async def test_raw_query_oversized_result_truncated(mocked_client):
    big = {"docker": {"containers": [{"id": "x" * 100} for _ in range(2000)]}}
    async with mocked_client(_resp(big)) as (client, _r):
        out = await misc.do_raw_query(client, "query { docker { containers { id } } }")
    assert out["truncated"] is True
    assert out["truncation_reason"] == "char_budget"
    assert "narrow the query selection" in out["message"].lower()
    assert "[truncated]" in out["preview"]
    assert _ser(out) <= MAX_RAW_RESULT_CHARS


async def test_raw_query_small_result_unchanged(mocked_client):
    async with mocked_client(_resp({"a": 1})) as (client, _r):
        assert await misc.do_raw_query(client, "query { a }") == {"a": 1}


async def test_budgeted_tools_advertise_meta(settings_factory):
    server = build_server(settings_factory())
    async with Client(server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    for name in ("get_docker_container_logs", "read_log_file"):
        assert tools[name].meta == {"anthropic/maxResultSizeChars": MAX_LOG_RESULT_CHARS}


# Protocol level: text block AND structured payload within budget.


def _short_lines():
    return _logs_payload(1000, 20)


def _quote_lines():
    payload = _logs_payload(1000, 0)
    for line in payload["docker"]["logs"]["lines"]:
        line["message"] = '"\\' * 995
    return payload


def _quote_file():
    return _log_file_payload("\n".join('"\\' * 1000 for _ in range(500)) + "\n")


def _quote_raw():
    return {"a": ['"\\' * 50_000]}


@pytest.mark.parametrize(
    "tool,arguments,data,flags",
    [
        ("get_docker_container_logs", {"container_id": "1:a", "tail": 1000}, _short_lines(), {}),
        ("get_docker_container_logs", {"container_id": "1:a", "tail": 1000}, _quote_lines(), {}),
        ("read_log_file", {"path": "/var/log/syslog", "lines": 500}, _quote_file(), {}),
        ("run_graphql_query", {"query": "query { a }"}, _quote_raw(), {"allow_raw_query": True}),
    ],
    ids=["docker-short", "docker-quotes", "logfile-quotes", "raw-quotes"],
)
async def test_protocol_text_and_structured_within_budget(tool, arguments, data, flags):
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(make_settings(**flags)), raise_exceptions=True) as s:
            result = await s.call_tool(tool, arguments)
    assert result.is_error is False, result.content
    assert len(result.content) == 1
    assert len(result.content[0].text) <= MAX_LOG_RESULT_CHARS
    assert _ser(result.structured_content) <= MAX_LOG_RESULT_CHARS
    assert result.structured_content["truncated"] is True
