"""Total character budget for log and raw-query results (#159)."""

from __future__ import annotations

import json

import httpx
from mcp.client import Client

from unraid_mcp.formatting import MAX_LOG_RESULT_CHARS, MAX_RAW_RESULT_CHARS
from unraid_mcp.server import build_server
from unraid_mcp.tools import docker, misc


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
    assert len(json.dumps(out)) <= MAX_LOG_RESULT_CHARS
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
    assert len(json.dumps(out)) <= MAX_LOG_RESULT_CHARS
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
    assert len(out["preview"]) <= MAX_RAW_RESULT_CHARS + 50


async def test_raw_query_small_result_unchanged(mocked_client):
    async with mocked_client(_resp({"a": 1})) as (client, _r):
        assert await misc.do_raw_query(client, "query { a }") == {"a": 1}


async def test_budgeted_tools_advertise_meta(settings_factory):
    server = build_server(settings_factory())
    async with Client(server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    for name in ("get_docker_container_logs", "read_log_file"):
        assert tools[name].meta == {"anthropic/maxResultSizeChars": MAX_LOG_RESULT_CHARS}
