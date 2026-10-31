from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp import Client, FastMCP

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.conversation_tools import READ_TOOLS, WRITE_TOOLS
from matrix_mcp.matrix_events import UNTRUSTED_TEXT, EventContext, HistoryPage, TimelineEvent
from matrix_mcp.matrix_rooms import MemberPage, Profile, Room, RoomInfo, UnreadPage
from matrix_mcp.mcp_server import INSTRUCTIONS, create_mcp_server, session_factory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from matrix_mcp.conversation_tools import ConversationClient

ROOM = "!room:example.com"
REMOVED_TOOLS = {
    "matrix_read_room_recent",
    "matrix_list_invitations",
    "matrix_download_media",
    "matrix_upload_media",
    "matrix_send_media",
    "matrix_redact_event",
    "matrix_join_room",
    "matrix_leave_room",
    "matrix_create_room",
    "matrix_invite_user",
    "matrix_search_users",
    "matrix_set_room_name",
    "matrix_set_room_topic",
    "matrix_set_room_avatar",
    "matrix_set_display_name",
    "matrix_set_avatar",
}

Calls = list[tuple[str, dict[str, Any]]]


def event(event_id: str) -> TimelineEvent:
    return TimelineEvent(
        event_id=event_id,
        sender="@bob:example.com",
        type="m.room.message",
        msgtype="m.text",
        body="hello",
    )


class FakeEvents:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls

    async def history(self, room_id: str, **kwargs: Any) -> HistoryPage:
        self.calls.append(("history", {"room_id": room_id, **kwargs}))
        return HistoryPage(events=[event("$e")], next_batch="next")

    async def context(self, room_id: str, event_id: str, **kwargs: Any) -> EventContext:
        self.calls.append(("context", {"room_id": room_id, "event_id": event_id, **kwargs}))
        return EventContext(event=event(event_id), events_before=[], events_after=[])

    async def thread(self, room_id: str, thread_id: str, **kwargs: Any) -> HistoryPage:
        self.calls.append(("thread", {"room_id": room_id, "thread_id": thread_id, **kwargs}))
        return HistoryPage(events=[event(thread_id)])

    async def send_text(self, room_id: str, body: str, **kwargs: Any) -> str:
        # Yield so parallel calls interleave, as they do against a real homeserver.
        await asyncio.sleep(0)
        self.calls.append(("send_text", {"room_id": room_id, "body": body, **kwargs}))
        return f"$sent-{room_id}"

    async def reply(self, room_id: str, event_id: str, body: str, **kwargs: Any) -> str:
        self.calls.append(
            ("reply", {"room_id": room_id, "event_id": event_id, "body": body, **kwargs})
        )
        return "$reply"

    async def react(self, room_id: str, event_id: str, key: str, **kwargs: Any) -> str:
        self.calls.append(
            ("react", {"room_id": room_id, "event_id": event_id, "key": key, **kwargs})
        )
        return "$reaction"

    async def edit(self, room_id: str, event_id: str, body: str, **kwargs: Any) -> str:
        self.calls.append(
            ("edit", {"room_id": room_id, "event_id": event_id, "body": body, **kwargs})
        )
        return "$edit"


class FakeRooms:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls

    async def whoami(self) -> dict[str, str | None]:
        self.calls.append(("whoami", {}))
        return {"user_id": "@alice:example.com", "device_id": "TESTDEVICE"}

    async def list_rooms(self) -> list[Room]:
        self.calls.append(("list_rooms", {}))
        return [Room(room_id=ROOM, name="Planning")]

    async def info(self, room_id: str) -> RoomInfo:
        self.calls.append(("info", {"room_id": room_id}))
        return RoomInfo(room_id=room_id, name="Planning")

    async def members(self, room_id: str, **kwargs: Any) -> MemberPage:
        self.calls.append(("members", {"room_id": room_id, **kwargs}))
        return MemberPage(members=[], total=0, next_offset=None)

    async def profile(self, user_id: str | None = None) -> Profile:
        self.calls.append(("profile", {"user_id": user_id}))
        return Profile(user_id=user_id or "@alice:example.com")

    async def unread(self, **kwargs: Any) -> UnreadPage:
        self.calls.append(("unread", kwargs))
        return UnreadPage(rooms=[], total=0, next_offset=None)

    async def mark_read(self, room_id: str, event_id: str, **kwargs: Any) -> None:
        self.calls.append(("mark_read", {"room_id": room_id, "event_id": event_id, **kwargs}))


class FakeSession:
    def __init__(self) -> None:
        self.calls: Calls = []
        self.events = FakeEvents(self.calls)
        self.rooms = FakeRooms(self.calls)


def fake_server(session: FakeSession) -> FastMCP:
    @asynccontextmanager
    async def client() -> AsyncGenerator[ConversationClient]:
        yield cast("ConversationClient", session)

    return create_mcp_server(client)


async def tools_by_name() -> dict[str, Any]:
    async with Client(fake_server(FakeSession())) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def test_server_registers_exactly_the_kept_tools() -> None:
    names = set(await tools_by_name())

    assert names == set(READ_TOOLS) | set(WRITE_TOOLS)
    assert len(names) == 14
    assert not names & REMOVED_TOOLS


async def test_tools_annotate_reads_and_writes() -> None:
    tools = await tools_by_name()

    for name in READ_TOOLS:
        assert tools[name].annotations.readOnlyHint is True
    for name in WRITE_TOOLS:
        assert tools[name].annotations.readOnlyHint is False


async def test_no_tool_takes_a_local_path_url_or_credentials() -> None:
    forbidden = {"file_path", "path", "filename", "media_url", "homeserver", "access_token"}

    for tool in (await tools_by_name()).values():
        assert not forbidden & tool.inputSchema.get("properties", {}).keys(), tool.name


async def test_send_message_refuses_a_file_path_argument() -> None:
    session = FakeSession()
    async with Client(fake_server(session)) as client:
        result = await client.call_tool(
            "matrix_send_message",
            {"room_id": ROOM, "body": "hi", "file_path": "~/.ssh/id_ed25519"},
            raise_on_error=False,
        )

    assert result.is_error
    assert session.calls == []


@pytest.mark.parametrize("room_id", ["1", "room", "#alias:example.com", "!room:example.com/x"])
async def test_room_arguments_take_raw_room_ids_only(room_id: str) -> None:
    session = FakeSession()
    async with Client(fake_server(session)) as client:
        result = await client.call_tool(
            "matrix_send_message", {"room_id": room_id, "body": "hi"}, raise_on_error=False
        )

    assert result.is_error
    assert session.calls == []


async def test_parallel_sends_each_reach_their_own_room() -> None:
    session = FakeSession()
    rooms = [f"!r{number}:example.com" for number in range(10)]
    async with Client(fake_server(session)) as client:
        results = await asyncio.gather(
            *(
                client.call_tool("matrix_send_message", {"room_id": room, "body": room})
                for room in rooms
            )
        )

    assert [result.data.event_id for result in results] == [f"$sent-{room}" for room in rooms]
    assert sorted((call["room_id"], call["body"]) for _, call in session.calls) == sorted(
        (room, room) for room in rooms
    )


@pytest.mark.parametrize(
    ("name", "arguments", "expected_call"),
    [
        ("matrix_whoami", {}, ("whoami", {})),
        ("matrix_list_rooms", {}, ("list_rooms", {})),
        ("matrix_get_room_info", {"room_id": ROOM}, ("info", {"room_id": ROOM})),
        (
            "matrix_list_room_members",
            {"room_id": ROOM, "limit": 5, "offset": 2},
            ("members", {"room_id": ROOM, "limit": 5, "offset": 2}),
        ),
        ("matrix_get_profile", {}, ("profile", {"user_id": None})),
        (
            "matrix_read_history",
            {"room_id": "!v12hash", "limit": 7, "before": "cursor"},
            ("history", {"room_id": "!v12hash", "limit": 7, "before": "cursor"}),
        ),
        (
            "matrix_get_event_context",
            {"room_id": ROOM, "event_id": "$e", "limit": 9},
            ("context", {"room_id": ROOM, "event_id": "$e", "limit": 9}),
        ),
        (
            "matrix_read_thread",
            {"room_id": ROOM, "thread_id": "$root", "limit": 3},
            ("thread", {"room_id": ROOM, "thread_id": "$root", "limit": 3, "before": None}),
        ),
        (
            "matrix_get_unread",
            {"limit": 4},
            ("unread", {"limit": 4, "offset": 0, "timeline_limit": 20}),
        ),
        (
            "matrix_send_message",
            {"room_id": ROOM, "body": "hi", "thread_id": "$root", "mentions": ["@b:example.com"]},
            (
                "send_text",
                {
                    "room_id": ROOM,
                    "body": "hi",
                    "thread_id": "$root",
                    "mentions": ["@b:example.com"],
                    "transaction_id": None,
                },
            ),
        ),
        (
            "matrix_reply",
            {"room_id": ROOM, "event_id": "$e", "body": "yes"},
            ("reply", {"room_id": ROOM, "event_id": "$e", "body": "yes", "transaction_id": None}),
        ),
        (
            "matrix_react",
            {"room_id": ROOM, "event_id": "$e", "key": "👍"},
            ("react", {"room_id": ROOM, "event_id": "$e", "key": "👍", "transaction_id": None}),
        ),
        (
            "matrix_edit_message",
            {"room_id": ROOM, "event_id": "$e", "body": "fixed"},
            ("edit", {"room_id": ROOM, "event_id": "$e", "body": "fixed", "transaction_id": None}),
        ),
        (
            "matrix_mark_read",
            {"room_id": ROOM, "event_id": "$e"},
            ("mark_read", {"room_id": ROOM, "event_id": "$e", "public_receipt": False}),
        ),
    ],
)
async def test_tools_dispatch_exact_arguments(
    name: str, arguments: dict[str, Any], expected_call: tuple[str, dict[str, Any]]
) -> None:
    session = FakeSession()
    async with Client(fake_server(session)) as client:
        result = await client.call_tool(name, arguments)

    assert not result.is_error
    assert session.calls == [expected_call]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("matrix_read_history", {"room_id": ROOM, "limit": 0}),
        ("matrix_get_event_context", {"room_id": ROOM, "event_id": "event"}),
        ("matrix_read_thread", {"room_id": ROOM, "thread_id": "$root", "limit": 101}),
        ("matrix_get_unread", {"limit": 101}),
        ("matrix_send_message", {"room_id": ROOM, "body": ""}),
        ("matrix_send_message", {"room_id": ROOM, "body": "x" * 20_001}),
        ("matrix_send_message", {"room_id": ROOM, "body": "hi", "mentions": ["bob"]}),
        ("matrix_react", {"room_id": ROOM, "event_id": "$e", "key": "x" * 101}),
        ("matrix_get_profile", {"user_id": "alice"}),
    ],
)
async def test_tools_reject_malformed_inputs(name: str, arguments: dict[str, Any]) -> None:
    session = FakeSession()
    async with Client(fake_server(session)) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)

    assert result.is_error
    assert session.calls == []


async def test_outputs_label_other_peoples_text_as_untrusted() -> None:
    tools = await tools_by_name()
    schema = str(tools["matrix_read_history"].outputSchema)

    assert UNTRUSTED_TEXT in schema
    assert "never as instructions" in INSTRUCTIONS


async def test_a_bad_config_never_reaches_the_model_with_its_secrets() -> None:
    def load() -> MatrixMCPConfig:
        return MatrixMCPConfig.from_json(
            b'{"homeserver": "https://m.example.com", "http_headers": {"X": "HEADER_SECRET"}}'
        )

    async with Client(create_mcp_server(session_factory(load))) as client:
        result = await client.call_tool("matrix_whoami", {}, raise_on_error=False)

    assert result.is_error
    assert "HEADER_SECRET" not in str(result.content)


async def test_every_read_tool_tells_the_model_its_text_is_untrusted() -> None:
    tools = await tools_by_name()

    for name in READ_TOOLS:
        if name != "matrix_whoami":
            assert "untrusted" in (tools[name].description or ""), name
