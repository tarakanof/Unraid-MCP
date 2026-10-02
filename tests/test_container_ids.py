"""Short container ids in output; full/bare/short/name ids on input (#172)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitResult

from unraid_mcp import queries
from unraid_mcp.formatting import short_container_id, shorten_container_ids
from unraid_mcp.server import build_server
from unraid_mcp.tools import docker

from .conftest import URL

SERVER = "f" * 64
PLEX = "4607a3dbd171" + "5" * 52
SONARR = "4607a3dbd172" + "6" * 52  # shares an 11-char prefix with PLEX
RADARR = "0123456789ab" + "7" * 52
FULL = f"{SERVER}:{PLEX}"

REFS = {
    "docker": {
        "containers": [
            {"id": f"{SERVER}:{PLEX}", "names": ["/plex"]},
            {"id": f"{SERVER}:{SONARR}", "names": ["/sonarr"]},
            {"id": f"{SERVER}:{RADARR}", "names": ["/radarr"]},
        ]
    }
}
DETAIL = {"docker": {"container": {"id": FULL, "names": ["/plex"], "state": "RUNNING"}}}
STARTED = {"docker": {"start": {"id": FULL, "names": ["/plex"], "state": "RUNNING"}}}
STOPPED = {"docker": {"stop": {"id": FULL, "names": ["/plex"], "state": "EXITED"}}}


def _resp(data):
    return httpx.Response(200, json={"data": data})


def _body(call):
    return json.loads(call.request.content)


# ── Output ───────────────────────────────────────────────────────────────────


def test_short_container_id_forms():
    assert short_container_id(FULL) == PLEX[:12]
    assert short_container_id(PLEX) == PLEX[:12]
    assert short_container_id(None) is None


def test_colliding_short_ids_fall_back_to_bare_64_hex():
    twin = PLEX[:12] + "0" * 52
    out = shorten_container_ids(
        [{"id": FULL}, {"id": f"{SERVER}:{twin}"}, {"id": f"{SERVER}:{RADARR}"}, None]
    )
    assert [o and o["id"] for o in out] == [PLEX, twin, RADARR[:12], None]


async def test_list_emits_short_ids(mocked_client):
    async with mocked_client(_resp(REFS)) as (client, _):
        out = await docker.fetch_containers(client, detail="concise")
    assert [c["id"] for c in out] == [PLEX[:12], SONARR[:12], RADARR[:12]]


async def test_port_conflicts_emit_short_ids(mocked_client):
    data = {
        "docker": {
            "portConflicts": {
                "containerPorts": [
                    {"privatePort": 80, "type": "TCP", "containers": [{"id": FULL, "name": "plex"}]}
                ],
                "lanPorts": [],
            }
        }
    }
    async with mocked_client(_resp(data)) as (client, _):
        out = await docker.fetch_docker_port_conflicts(client)
    assert out["container_ports"][0]["containers"] == [{"id": PLEX[:12], "name": "plex"}]


# ── Read input forms ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("ident", [FULL, PLEX])
async def test_get_container_full_and_bare_ids_pass_through(mocked_client, ident):
    async with mocked_client(_resp(DETAIL)) as (client, route):
        out = await docker.fetch_container(client, ident)
    assert route.call_count == 1
    assert _body(route.calls[0])["variables"] == {"id": ident}
    assert out["id"] == PLEX[:12]


@pytest.mark.parametrize("ident", [PLEX[:12], PLEX[:20].upper(), "plex", "/plex"])
async def test_get_container_short_id_and_name_resolve(mocked_client, ident):
    async with mocked_client([_resp(REFS), _resp(DETAIL)]) as (client, route):
        out = await docker.fetch_container(client, ident)
    assert route.call_count == 2
    assert _body(route.calls[1])["variables"] == {"id": PLEX}
    assert out["id"] == PLEX[:12]


async def test_get_container_below_minimum_short_id_not_found(mocked_client):
    async with mocked_client(_resp(REFS)) as (client, route):
        with pytest.raises(ToolError, match="at least 12 hex chars"):
            await docker.fetch_container(client, PLEX[:11])
    assert route.call_count == 1


async def test_get_container_ambiguous_short_id_lists_candidates(mocked_client):
    twin = PLEX[:12] + "0" * 52
    refs = {"docker": {"containers": [*REFS["docker"]["containers"], {"id": f"{SERVER}:{twin}"}]}}
    async with mocked_client(_resp(refs)) as (client, route):
        with pytest.raises(ToolError) as exc:
            await docker.fetch_container(client, PLEX[:12])
    msg = str(exc.value)
    assert "matches 2 containers" in msg and PLEX in msg and twin in msg and "plex" in msg
    assert SERVER not in msg
    # Only the list lookup was made, never a detail query for a guessed id.
    assert route.call_count == 1


async def test_logs_short_id_resolves_before_query(mocked_client):
    logs = {"docker": {"logs": {"containerId": FULL, "lines": [], "cursor": None}}}
    async with mocked_client([_resp(REFS), _resp(logs)]) as (client, route):
        out = await docker.fetch_container_logs(client, PLEX[:12], tail=5)
    assert _body(route.calls[0])["query"] == queries.CONTAINER_REFS
    assert _body(route.calls[1])["variables"] == {"id": PLEX, "since": None, "tail": 5}
    assert out["container_id"] == PLEX[:12]


# ── Mutation input forms ─────────────────────────────────────────────────────


@pytest.mark.parametrize("ident", [FULL, PLEX])
async def test_mutation_full_and_bare_ids_send_without_lookup(mocked_client, ident):
    async with mocked_client(_resp(STOPPED)) as (client, route):
        out = await docker.do_stop_container(client, ident, confirm=True)
    assert route.call_count == 1
    assert _body(route.calls[0])["variables"] == {"id": ident}
    assert out["id"] == PLEX[:12]


@pytest.mark.parametrize(
    "call",
    [
        lambda c, i: docker.do_stop_container(c, i, confirm=True),
        lambda c, i: docker.do_restart_container(c, i, confirm=True),
        lambda c, i: docker.do_container_power(c, i, "start", confirm=True),
        lambda c, i: docker.do_container_power(c, i, "pause", confirm=True),
        lambda c, i: docker.do_update_container(c, i, confirm=True),
        lambda c, i: docker.do_remove_container(c, i, confirm=True),
    ],
    ids=["stop", "restart", "start", "pause", "update", "remove"],
)
async def test_mutation_short_id_resolves_after_confirm(mocked_client, call):
    ok = {
        "docker": {
            k: {"id": FULL, "names": ["/plex"]}
            for k in ("stop", "restart", "start", "pause", "updateContainer")
        }
    }
    ok["docker"]["removeContainer"] = True
    async with mocked_client([_resp(REFS), _resp(ok)]) as (client, route):
        await call(client, PLEX[:12])
    assert _body(route.calls[0])["query"] == queries.CONTAINER_REFS
    assert _body(route.calls[1])["variables"]["id"] == PLEX
    assert route.call_count == 2


@pytest.mark.parametrize("ident", [PLEX[:12], "plex", FULL, PLEX])
@pytest.mark.parametrize(
    "call",
    [
        lambda c, i: docker.do_stop_container(c, i, confirm=False),
        lambda c, i: docker.do_restart_container(c, i, confirm=False),
        lambda c, i: docker.do_container_power(c, i, "start", confirm=False),
        lambda c, i: docker.do_update_container(c, i, confirm=False),
        lambda c, i: docker.do_update_containers(c, [i], confirm=False),
        lambda c, i: docker.do_remove_container(c, i, confirm=False),
        lambda c, i: docker.do_set_docker_autostart(
            c, [{"id": i, "auto_start": True}], confirm=False
        ),
    ],
    ids=["stop", "restart", "power", "update", "update_many", "remove", "autostart"],
)
async def test_mutation_without_confirm_makes_no_request(mocked_client, call, ident):
    async with mocked_client(_resp(REFS)) as (client, route):
        with pytest.raises(ToolError, match="confirm=true"):
            await call(client, ident)
    assert route.call_count == 0


async def test_ambiguous_short_id_makes_no_mutation_request(mocked_client):
    twin = PLEX[:12] + "0" * 52
    refs = {"docker": {"containers": [*REFS["docker"]["containers"], {"id": twin}]}}
    async with mocked_client(_resp(refs)) as (client, route):
        with pytest.raises(ToolError, match="matches 2 containers"):
            await docker.do_stop_container(client, PLEX[:12], confirm=True)
        with pytest.raises(ToolError, match="matches 2 containers"):
            await docker.do_update_containers(client, [RADARR[:12], PLEX[:12]], confirm=True)
    assert all(_body(c)["query"] == queries.CONTAINER_REFS for c in route.calls)


async def test_unknown_short_id_makes_no_mutation_request(mocked_client):
    async with mocked_client(_resp(REFS)) as (client, route):
        with pytest.raises(ToolError, match="No Docker container"):
            await docker.do_remove_container(client, "deadbeefdead", confirm=True)
    assert route.call_count == 1


async def test_update_containers_mixed_forms_one_lookup(mocked_client):
    updated = {"docker": {"updateContainers": [{"id": FULL}, {"id": f"{SERVER}:{RADARR}"}]}}
    async with mocked_client([_resp(REFS), _resp(updated)]) as (client, route):
        out = await docker.do_update_containers(client, [PLEX[:12], RADARR], confirm=True)
    assert route.call_count == 2
    assert _body(route.calls[1])["variables"] == {"ids": [PLEX, RADARR]}
    assert [c["id"] for c in out] == [PLEX[:12], RADARR[:12]]


async def test_autostart_accepts_short_and_bare_ids(mocked_client):
    state = {
        "docker": {
            "containers": [
                {"id": FULL, "names": ["/plex"], "autoStart": True, "autoStartOrder": 0},
                {"id": f"{SERVER}:{RADARR}", "names": ["/radarr"], "autoStart": False},
            ]
        }
    }
    done = {"docker": {"updateAutostartConfiguration": True}}
    async with mocked_client([_resp(state), _resp(done)]) as (client, route):
        out = await docker.do_set_docker_autostart(
            client,
            [{"id": RADARR[:12], "auto_start": True}],
            order=[RADARR, PLEX[:12]],
            confirm=True,
        )
    sent = _body(route.calls[1])["variables"]["entries"]
    assert [e["id"] for e in sent] == [RADARR, PLEX]
    assert [e["id"] for e in out["autostart"]] == [RADARR[:12], PLEX[:12]]


async def test_autostart_same_container_twice_via_different_forms_rejected(mocked_client):
    state = {"docker": {"containers": [{"id": FULL, "names": ["/plex"], "autoStart": False}]}}
    async with mocked_client(_resp(state)) as (client, route):
        with pytest.raises(ToolError, match="more than once"):
            await docker.do_set_docker_autostart(
                client,
                [{"id": PLEX[:12], "auto_start": True}, {"id": FULL, "auto_start": False}],
                confirm=True,
            )
    assert route.call_count == 1  # state read only, no mutation


# ── Protocol: elicitation shows the caller's id; lookup happens after it ─────


async def test_destructive_short_id_elicits_with_caller_id_before_any_request(settings_factory):
    messages = []

    async def elicit(context, params):
        assert len(respx.calls) == 0
        messages.append(params.message)
        return ElicitResult(action="accept", content={"proceed": True})

    with respx.mock:
        route = respx.post(URL).mock(side_effect=[_resp({}), _resp(REFS), _resp(STOPPED)])
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, mode="auto", elicitation_callback=elicit) as client:
            respx.calls.clear()
            result = await client.call_tool(
                "stop_docker_container", {"container_id": PLEX[:12], "confirm": True}
            )
    assert not result.is_error, result.content
    assert any(f"stop container '{PLEX[:12]}'" in m for m in messages)
    sent = [_body(c) for c in route.calls[1:]]
    assert sent[0]["query"] == queries.CONTAINER_REFS
    assert sent[1]["variables"] == {"id": PLEX}


# ── Review follow-ups ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ids",
    [
        [FULL, FULL],
        [PLEX, FULL],
        [PLEX.upper(), PLEX],
        [f" {PLEX} ", f"{SERVER}:{PLEX.upper()}"],
    ],
    ids=["full-full", "bare-full", "case", "space-prefixed-upper"],
)
async def test_update_containers_literal_alias_duplicates_rejected_before_any_request(
    mocked_client, ids
):
    async with mocked_client(_resp(REFS)) as (client, route):
        with pytest.raises(ToolError, match="more than once"):
            await docker.do_update_containers(client, ids, confirm=True)
    assert route.call_count == 0


@pytest.mark.parametrize(
    "ids",
    [[PLEX[:12], FULL], [PLEX[:12], PLEX], [PLEX[:12], PLEX[:16].upper()]],
    ids=["short-full", "short-bare", "short-short"],
)
async def test_update_containers_resolved_duplicates_rejected_before_mutation(mocked_client, ids):
    async with mocked_client(_resp(REFS)) as (client, route):
        with pytest.raises(ToolError, match="more than once") as exc:
            await docker.do_update_containers(client, ids, confirm=True)
    assert route.call_count == 1
    assert _body(route.calls[0])["query"] == queries.CONTAINER_REFS
    assert SERVER not in str(exc.value)


async def test_update_containers_literal_duplicates_refused_before_elicitation(settings_factory):
    async def elicit(context, params):
        pytest.fail("duplicates must be refused before asking a human")

    with respx.mock:
        respx.post(URL).mock(return_value=_resp({}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, mode="auto", elicitation_callback=elicit) as client:
            respx.calls.clear()
            result = await client.call_tool(
                "update_docker_containers", {"container_ids": [PLEX, FULL], "confirm": True}
            )
            assert result.is_error
            assert len(respx.calls) == 0


@pytest.mark.parametrize("ident", [f"  {FULL.upper()} ", f"{PLEX.upper()}\n"])
async def test_passthrough_ids_are_trimmed_and_lowercased(mocked_client, ident):
    async with mocked_client(_resp(STOPPED)) as (client, route):
        await docker.do_stop_container(client, ident, confirm=True)
    sent = _body(route.calls[0])["variables"]["id"]
    assert sent == sent.strip()
    assert sent.split(":")[-1] == PLEX


async def test_hex_looking_name_reachable_by_exact_name(mocked_client):
    hex_name = "d" * 64
    rows = {
        "docker": {
            "containers": [
                {"id": FULL, "names": [f"/{hex_name}"], "state": "RUNNING"},
                {"id": f"{SERVER}:{RADARR}", "names": ["/radarr"]},
            ]
        }
    }
    responses = [_resp({"docker": {"container": None}}), _resp(rows), _resp(DETAIL)]
    async with mocked_client(responses) as (client, route):
        out = await docker.fetch_container(client, hex_name)
    assert out["id"] == PLEX[:12]
    assert _body(route.calls[0])["variables"] == {"id": hex_name}
    assert _body(route.calls[2])["variables"] == {"id": PLEX}


async def test_hex_looking_short_name_reachable(mocked_client):
    rows = {"docker": {"containers": [{"id": FULL, "names": ["/cafebabe1234"]}]}}
    async with mocked_client([_resp(rows), _resp(DETAIL)]) as (client, route):
        out = await docker.fetch_container(client, "cafebabe1234")
    assert out["id"] == PLEX[:12]
    assert _body(route.calls[1])["variables"] == {"id": PLEX}


AUTOSTART_STATE = {
    "docker": {
        "containers": [
            {"id": FULL, "names": ["/plex"], "autoStart": True, "autoStartOrder": 0},
            {"id": f"{SERVER}:{RADARR}", "names": ["/radarr"], "autoStart": False},
        ]
    }
}


@pytest.mark.parametrize(
    "entries,order",
    [
        ([{"id": "deadbeefdead", "auto_start": True}], None),
        ([{"id": f"{SERVER}:{'e' * 64}", "auto_start": True}], None),
        ([{"id": PLEX[:12], "auto_start": False}], [PLEX[:12]]),
        ([{"id": RADARR[:12], "auto_start": True}], [RADARR[:12], FULL, PLEX]),
        ([{"id": PLEX[:12], "auto_start": True}, {"id": FULL, "auto_start": True}], None),
    ],
    ids=["unknown-short", "unknown-full", "order-not-enabled", "order-dup", "entries-dup"],
)
async def test_autostart_errors_never_show_server_prefix(mocked_client, entries, order):
    async with mocked_client(_resp(AUTOSTART_STATE)) as (client, route):
        with pytest.raises(ToolError) as exc:
            await docker.do_set_docker_autostart(client, entries, order=order, confirm=True)
    msg = str(exc.value)
    assert SERVER not in msg
    assert PLEX not in msg and RADARR not in msg  # short form only
    assert route.call_count <= 1  # at most the state read; never the mutation


async def test_check_docker_updates_ids_feed_update_tools(mocked_client):
    data = {
        "docker": {
            "containerUpdateStatuses": [{"name": "plex", "updateStatus": "UPDATE_AVAILABLE"}],
            "containers": REFS["docker"]["containers"],
        }
    }
    async with mocked_client(_resp(data)) as (client, _):
        (status,) = await docker.fetch_docker_updates(client)
    assert status == {"id": PLEX[:12], "name": "plex", "update_status": "UPDATE_AVAILABLE"}


@pytest.mark.parametrize(
    "entry_id,order",
    [
        (f" {RADARR[:12]} ", None),
        (f"\t{RADARR.upper()} ", None),
        (RADARR[:12], [f" {RADARR[:12]} ", f" {FULL.upper()} "]),
        (RADARR, [f" {RADARR} ", f"{PLEX[:12]}\n"]),
    ],
    ids=["entry-short", "entry-full", "order-short", "order-full"],
)
async def test_autostart_padded_ids_are_trimmed(mocked_client, entry_id, order):
    done = {"docker": {"updateAutostartConfiguration": True}}
    async with mocked_client([_resp(AUTOSTART_STATE), _resp(done)]) as (client, route):
        out = await docker.do_set_docker_autostart(
            client, [{"id": entry_id, "auto_start": True}], order=order, confirm=True
        )
    sent = [e["id"] for e in _body(route.calls[1])["variables"]["entries"]]
    expected = [RADARR, PLEX] if order else [PLEX, RADARR]
    assert sent == expected
    assert {e["id"] for e in out["autostart"]} == {PLEX[:12], RADARR[:12]}


@pytest.mark.parametrize(
    "entries,order",
    [
        ([{"id": f" {'e' * 64} ", "auto_start": True}], None),
        ([{"id": " deadbeefdead ", "auto_start": True}], None),
        ([{"id": RADARR[:12], "auto_start": True}], [f" {RADARR} ", RADARR.upper()]),
        ([{"id": f" {PLEX} ", "auto_start": True}, {"id": PLEX, "auto_start": False}], None),
    ],
    ids=["unknown-full", "unknown-short", "order-dup", "entries-dup"],
)
async def test_autostart_padded_id_errors_show_short_form(mocked_client, entries, order):
    async with mocked_client(_resp(AUTOSTART_STATE)) as (client, route):
        with pytest.raises(ToolError) as exc:
            await docker.do_set_docker_autostart(client, entries, order=order, confirm=True)
    msg = str(exc.value)
    assert "e" * 13 not in msg and PLEX not in msg and RADARR not in msg.lower()
    assert SERVER not in msg
    assert route.call_count <= 1
