from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.matrix_http import MatrixHTTP
from matrix_mcp.matrix_rooms import MatrixRooms

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ROOM = "!room:example.com"
ROOM_PATH = f"/_matrix/client/v3/rooms/{ROOM}"


@dataclass
class RoomEndpoint:
    responses: dict[tuple[str, str], tuple[object, int]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": await request.json() if request.can_read_body else None,
            }
        )
        data, status = self.responses.get(
            (request.method, request.path), ({"errcode": "M_NOT_FOUND"}, 404)
        )
        return web.json_response(data, status=status)


@pytest.fixture
async def matrix() -> AsyncIterator[tuple[MatrixRooms, RoomEndpoint]]:
    endpoint = RoomEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        http = MatrixHTTP(
            MatrixMCPConfig(
                homeserver=str(server.make_url("/")),
                user_id="@alice:example.com",
                device_id="TESTDEVICE",
                access_token="test-token",
            )
        )
        yield MatrixRooms(http), endpoint


@pytest.mark.parametrize("public_receipt", [False, True])
async def test_mark_read_defaults_to_private_receipt(
    matrix: tuple[MatrixRooms, RoomEndpoint], *, public_receipt: bool
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$last")] = (
        {
            "event_id": "$last",
            "sender": "@bob:example.com",
            "type": "m.room.message",
            "content": {"body": "hello", "msgtype": "m.text"},
        },
        200,
    )
    endpoint.responses[("POST", f"{ROOM_PATH}/read_markers")] = ({}, 200)
    marked_unread_path = (
        f"/_matrix/client/v3/user/@alice:example.com/rooms/{ROOM}/account_data/m.marked_unread"
    )
    endpoint.responses[("PUT", marked_unread_path)] = ({}, 200)
    if public_receipt:
        await rooms.mark_read(ROOM, "$last", public_receipt=True)
    else:
        await rooms.mark_read(ROOM, "$last")
    receipt = "m.read" if public_receipt else "m.read.private"
    assert endpoint.requests[-2]["body"] == {"m.fully_read": "$last", receipt: "$last"}
    clear_request = endpoint.requests[-1]
    assert clear_request["method"] == "PUT"
    assert clear_request["path"] == marked_unread_path
    assert clear_request["body"] == {"unread": False}


async def test_denied_event_never_updates_read_state(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$last")] = (
        {"errcode": "M_FORBIDDEN"},
        403,
    )
    with pytest.raises(RuntimeError, match="M_FORBIDDEN"):
        await rooms.mark_read(ROOM, "$last")
    assert [request["method"] for request in endpoint.requests] == ["GET"]


async def test_mark_read_reports_partial_success_when_unread_clear_fails(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$last")] = (
        {
            "event_id": "$last",
            "sender": "@bob:example.com",
            "type": "m.room.message",
            "content": {"body": "hello", "msgtype": "m.text"},
        },
        200,
    )
    endpoint.responses[("POST", f"{ROOM_PATH}/read_markers")] = ({}, 200)
    marked_unread_path = (
        f"/_matrix/client/v3/user/@alice:example.com/rooms/{ROOM}/account_data/m.marked_unread"
    )
    endpoint.responses[("PUT", marked_unread_path)] = (
        {"errcode": "M_UNKNOWN", "error": "do-not-echo-this"},
        500,
    )

    with pytest.raises(RuntimeError, match=r"read markers were updated.*safe to retry") as error:
        await rooms.mark_read(ROOM, "$last")

    assert "do-not-echo-this" not in str(error.value)
    assert [request["method"] for request in endpoint.requests] == ["GET", "POST", "PUT"]


async def test_mark_read_does_not_clear_unread_after_marker_failure(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$last")] = (
        {
            "event_id": "$last",
            "sender": "@bob:example.com",
            "type": "m.room.message",
            "content": {"body": "hello", "msgtype": "m.text"},
        },
        200,
    )
    endpoint.responses[("POST", f"{ROOM_PATH}/read_markers")] = (
        {"errcode": "M_FORBIDDEN"},
        403,
    )

    with pytest.raises(RuntimeError, match="M_FORBIDDEN"):
        await rooms.mark_read(ROOM, "$last")

    assert [request["method"] for request in endpoint.requests] == ["GET", "POST"]


async def test_unread_returns_counts_mentions_and_server_cursors_without_marking(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    mention = {
        "event_id": "$mention",
        "sender": "@bob:example.com",
        "origin_server_ts": 123,
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": "Can you help?",
            "m.mentions": {"user_ids": ["@alice:example.com"]},
        },
    }
    endpoint.responses[("GET", "/_matrix/client/v3/sync")] = (
        {
            "next_batch": "next",
            "rooms": {
                "join": {
                    ROOM: {
                        "state": {
                            "events": [{"type": "m.room.name", "content": {"name": "Planning"}}]
                        },
                        "timeline": {"events": [mention], "limited": True, "prev_batch": "older"},
                        "unread_notifications": {"notification_count": 3, "highlight_count": 1},
                    },
                    "!read:example.com": {"timeline": {"events": [mention]}},
                }
            },
        },
        200,
    )
    page = await rooms.unread(timeline_limit=7)
    assert set(page.model_dump()) == {"rooms", "total", "next_offset"}
    assert page.total == 1
    room = page.rooms[0]
    assert room.name == "Planning"
    assert room.notification_count == 3
    assert room.highlight_count == 1
    assert [event.event_id for event in room.mentions] == ["$mention"]
    assert room.limited is True
    assert room.prev_batch == "older"
    request = endpoint.requests[0]
    assert "since" not in request["query"]
    sync_filter = json.loads(request["query"]["filter"])
    assert sync_filter["event_fields"] == [
        "event_id",
        "sender",
        "origin_server_ts",
        "state_key",
        "type",
        "content.body",
        "content.m\\.mentions",
        "content.name",
        "content.unread",
    ]
    assert sync_filter["room"]["timeline"]["limit"] == 7
    assert sync_filter["room"]["timeline"]["types"] == ["m.room.message"]
    assert request["query"]["set_presence"] == "offline"
    assert [request["method"] for request in endpoint.requests] == ["GET"]


@pytest.mark.parametrize("method", ["unread", "members"])
@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_sync_page_bounds_reject_before_http(
    matrix: tuple[MatrixRooms, RoomEndpoint], method: str, arguments: dict[str, int]
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match="limit"):
        await getattr(rooms, method)(*([ROOM] if method == "members" else []), **arguments)
    assert not endpoint.requests


async def test_malformed_sync_is_an_error(matrix: tuple[MatrixRooms, RoomEndpoint]) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", "/_matrix/client/v3/sync")] = ({"rooms": []}, 200)
    with pytest.raises(RuntimeError, match=r"invalid|Malformed"):
        await rooms.unread()


async def test_whoami_asks_the_homeserver(matrix: tuple[MatrixRooms, RoomEndpoint]) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", "/_matrix/client/v3/account/whoami")] = (
        {"user_id": "@alice:example.com", "device_id": "TESTDEVICE"},
        200,
    )

    assert await rooms.whoami() == {"user_id": "@alice:example.com", "device_id": "TESTDEVICE"}
    assert endpoint.requests[0]["headers"]["Authorization"] == "Bearer test-token"


async def test_list_rooms_names_each_room_and_tolerates_unnamed_ones(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", "/_matrix/client/v3/joined_rooms")] = (
        {"joined_rooms": ["!b:example.com", ROOM]},
        200,
    )
    endpoint.responses[("GET", f"{ROOM_PATH}/state/m.room.name/")] = ({"name": "Planning"}, 200)

    listed = await rooms.list_rooms()

    assert [(room.room_id, room.name) for room in listed] == [
        ("!b:example.com", None),
        (ROOM, "Planning"),
    ]


async def test_list_rooms_refuses_more_than_its_room_cap(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", "/_matrix/client/v3/joined_rooms")] = (
        {"joined_rooms": [f"!r{number}:example.com" for number in range(501)]},
        200,
    )

    with pytest.raises(RuntimeError, match="500"):
        await rooms.list_rooms()

    assert len(endpoint.requests) == 1


async def test_room_state_errors_other_than_not_found_are_raised(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/state/m.room.name/")] = (
        {"errcode": "M_FORBIDDEN", "error": "secret server text"},
        403,
    )

    with pytest.raises(RuntimeError, match="HTTP 403") as raised:
        await rooms.info(ROOM)

    assert "secret server text" not in str(raised.value)


async def test_members_are_sorted_and_paged(matrix: tuple[MatrixRooms, RoomEndpoint]) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/joined_members")] = (
        {
            "joined": {
                "@carol:example.com": {"display_name": "Carol"},
                "@alice:example.com": {"display_name": "Alice", "avatar_url": "mxc://e/a"},
                "@bob:example.com": {},
            }
        },
        200,
    )

    page = await rooms.members(ROOM, limit=2, offset=1)

    assert [member.user_id for member in page.members] == [
        "@bob:example.com",
        "@carol:example.com",
    ]
    assert page.total == 3
    assert page.next_offset is None


@pytest.mark.parametrize("user_id", ["alice", "@alice:example.com/../x", "@alice"])
async def test_profile_rejects_invalid_user_ids_before_http(
    matrix: tuple[MatrixRooms, RoomEndpoint], user_id: str
) -> None:
    rooms, endpoint = matrix

    with pytest.raises(ValueError, match="Invalid Matrix"):
        await rooms.profile(user_id)

    assert not endpoint.requests


async def test_avatar_urls_that_are_not_mxc_become_null(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", "/_matrix/client/v3/profile/@bob:example.com")] = (
        {"displayname": "Bob", "avatar_url": "https://tracker.example.com/pixel.gif"},
        200,
    )

    profile = await rooms.profile("@bob:example.com")

    assert profile.avatar_url is None
    assert profile.displayname == "Bob"
