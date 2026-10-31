"""Run the real `matrix-mcp serve` over stdio against a fake homeserver."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from matrix_mcp.config import MatrixMCPConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

ROOM = "!room:example.com"


@dataclass
class Homeserver:
    requests: list[tuple[str, str]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path))
        responses: dict[tuple[str, str], object] = {
            ("GET", "/_matrix/client/v3/account/whoami"): {
                "user_id": "@alice:example.com",
                "device_id": "TESTDEVICE",
            },
            ("GET", "/_matrix/client/v3/joined_rooms"): {"joined_rooms": [ROOM]},
            ("GET", f"/_matrix/client/v3/rooms/{ROOM}/state/m.room.name/"): {"name": "Planning"},
            ("PUT", f"/_matrix/client/v3/rooms/{ROOM}/send/m.room.message/txn"): {
                "event_id": "$sent"
            },
        }
        data = responses.get((request.method, request.path))
        if data is None:
            return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)
        return web.json_response(data)


@pytest.fixture
async def homeserver() -> AsyncIterator[tuple[str, Homeserver]]:
    fake = Homeserver()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", fake.handle)
    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/"), fake


def stdio_client(home: Path) -> Client[StdioTransport]:
    environment = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": ""}
    return Client(
        StdioTransport(
            command=sys.executable,
            args=["-c", "from matrix_mcp.cli import app; app()", "serve"],
            env=environment,
        )
    )


async def test_serve_answers_reads_and_a_send_over_stdio(
    homeserver: tuple[str, Homeserver], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, fake = homeserver
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    from matrix_mcp.config import default_config_path  # noqa: PLC0415 - After HOME changes.

    MatrixMCPConfig(
        homeserver=url,
        user_id="@alice:example.com",
        device_id="TESTDEVICE",
        access_token="test-token",
    ).save(default_config_path())

    async with stdio_client(tmp_path) as client:
        names = {tool.name for tool in await client.list_tools()}
        whoami = await client.call_tool("matrix_whoami", {})
        rooms = await client.call_tool("matrix_list_rooms", {})
        sent = await client.call_tool(
            "matrix_send_message",
            {"room_id": ROOM, "body": "hello", "transaction_id": "txn"},
        )

    assert len(names) == 14
    assert whoami.structured_content == {
        "user_id": "@alice:example.com",
        "device_id": "TESTDEVICE",
    }
    assert rooms.structured_content == {"result": [{"room_id": ROOM, "name": "Planning"}]}
    assert sent.structured_content == {"event_id": "$sent"}
    assert ("PUT", f"/_matrix/client/v3/rooms/{ROOM}/send/m.room.message/txn") in fake.requests


async def test_serve_refuses_a_token_file_other_users_can_read(
    homeserver: tuple[str, Homeserver], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, fake = homeserver
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    from matrix_mcp.config import default_config_path  # noqa: PLC0415 - After HOME changes.

    path = default_config_path()
    MatrixMCPConfig(
        homeserver=url, user_id="@alice:example.com", device_id="D", access_token="t"
    ).save(path)
    path.chmod(0o644)

    async with stdio_client(tmp_path) as client:
        result: Any = await client.call_tool("matrix_whoami", {}, raise_on_error=False)

    assert result.is_error
    assert fake.requests == []
