"""Shared mocked payloads for the compact-result tests (issue #156)."""

from __future__ import annotations

from typing import Any

from tests.test_output_schemas import _fixture


def twenty_containers() -> dict[str, Any]:
    """20 containers like a typical box: optional fields often null."""
    containers = []
    for i in range(20):
        containers.append(
            {
                "id": f"1:{i:064x}",
                "names": [f"/app{i}"],
                "image": f"lscr.io/linuxserver/app{i}:latest",
                "state": "RUNNING" if i % 3 else "EXITED",
                "status": "Up 3 days" if i % 3 else "Exited (0) 2 days ago",
                "autoStart": bool(i % 2),
                "isUpdateAvailable": None,
                "isOrphaned": None,
                "webUiUrl": f"http://tower:{8000 + i}/" if i % 4 == 0 else None,
                "autoStartOrder": None,
                "hostConfig": {"networkMode": "bridge"} if i % 5 == 0 else None,
                "ports": [
                    {"ip": "0.0.0.0", "privatePort": 80, "publicPort": 8000 + i, "type": "TCP"}
                ]
                if i % 2
                else [],
            }
        )
    return {"docker": {"containers": containers}}


# (golden key, tool, arguments, mocked GraphQL ``data``)
CASES: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
    (
        "list_docker_containers_20",
        "list_docker_containers",
        {"detail": "full"},
        twenty_containers(),
    ),
    (
        "list_docker_containers_full",
        "list_docker_containers",
        {"detail": "full"},
        _fixture("full")[0],
    ),
    (
        "list_docker_containers_null",
        "list_docker_containers",
        {"detail": "full"},
        _fixture("null")[0],
    ),
    ("get_health_summary_full", "get_health_summary", {}, _fixture("full")[0]),
    ("get_health_summary_null", "get_health_summary", {}, _fixture("null")[0]),
    ("list_disks_full", "list_disks", {"detail": "full"}, _fixture("full")[0]),
    ("list_disks_null", "list_disks", {"detail": "full"}, _fixture("null")[0]),
]
