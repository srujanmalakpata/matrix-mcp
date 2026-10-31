from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.matrix_http import MatrixHTTP, MatrixHTTPError, post_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


ROOM = "!room:example.com"
ROOM_PATH = "/prefix/_matrix/client/v3/rooms/!room:example.com"
ROOM_V12 = "!AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@dataclass
class MatrixEndpoint:
    responses: dict[tuple[str, str], list[tuple[object, int, dict[str, str]]]] = field(
        default_factory=dict
    )
    requests: list[dict[str, Any]] = field(default_factory=list)

    def respond(
        self,
        method: str,
        path: str,
        data: object,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.responses.setdefault((method, path), []).append((data, status, headers or {}))

    async def handle(self, request: web.Request) -> web.Response:
        body: object = None
        if request.can_read_body:
            body = (
                await request.json()
                if request.content_type == "application/json"
                else await request.read()
            )
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        queued = self.responses.get((request.method, request.path))
        if not queued:
            return web.json_response({"errcode": "M_NOT_FOUND", "error": "missing"}, status=404)
        data, status, headers = queued.pop(0)
        if isinstance(data, bytes):
            return web.Response(body=data, status=status, headers=headers)
        if isinstance(data, str):
            return web.Response(text=data, status=status, headers=headers)
        return web.json_response(data, status=status, headers=headers)


@pytest.fixture
async def matrix_http() -> AsyncIterator[tuple[MatrixHTTP, MatrixEndpoint]]:
    endpoint = MatrixEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        config = MatrixMCPConfig(
            homeserver=str(server.make_url("/prefix")),
            user_id="@alice:example.com",
            device_id="TESTDEVICE",
            access_token="test-token",
        )
        yield MatrixHTTP(config), endpoint


async def test_json_preserves_homeserver_prefix_auth_headers_query_and_body(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond("POST", f"{ROOM_PATH}/messages", {"ok": True})

    payload = await http.json(
        "POST",
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages",
        params={"from": "opaque+cursor", "limit": 7},
        body={"body": "hello"},
    )

    assert payload == {"ok": True}
    assert http.user_id == "@alice:example.com"
    assert endpoint.requests == [
        {
            "method": "POST",
            "path": f"{ROOM_PATH}/messages",
            "query": {"from": "opaque+cursor", "limit": "7"},
            "headers": endpoint.requests[0]["headers"],
            "body": {"body": "hello"},
        }
    ]
    assert endpoint.requests[0]["headers"]["Authorization"] == "Bearer test-token"
    assert endpoint.requests[0]["headers"]["Accept-Encoding"] == "identity"


async def test_client_exposes_configured_homeserver_as_base_url(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, _ = matrix_http

    async with http.client() as client:
        assert str(client.base_url).endswith("/prefix/")


async def test_json_rejects_redirect_without_exposing_location(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        {},
        status=302,
        headers={"Location": "https://private.example.com/secret"},
    )

    with pytest.raises(MatrixHTTPError, match="redirect") as caught:
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")

    assert caught.value.status_code == 302
    assert "private.example.com" not in str(caught.value)
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize(
    ("status", "errcode"),
    [(401, "M_UNKNOWN_TOKEN"), (403, "M_FORBIDDEN")],
)
async def test_json_preserves_status_and_errcode_without_server_error(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint], status: int, errcode: str
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        {"errcode": errcode, "error": "sensitive upstream detail"},
        status=status,
    )

    with pytest.raises(MatrixHTTPError) as caught:
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")

    assert caught.value.status_code == status
    assert caught.value.errcode == errcode
    assert "sensitive upstream detail" not in str(caught.value)


@pytest.mark.parametrize(
    "errcode",
    [
        "arbitrary-sentinel",
        "M_FORBIDDEN\nsentinel",
        "M_" + "A" * 81,
    ],
)
async def test_json_omits_unsafe_upstream_errcodes(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint], errcode: str
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        {"errcode": errcode, "error": "sensitive upstream detail"},
        status=403,
    )

    with pytest.raises(MatrixHTTPError) as caught:
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")

    assert caught.value.status_code == 403
    assert caught.value.errcode is None
    assert errcode not in str(caught.value)
    assert "sensitive upstream detail" not in str(caught.value)


async def test_json_rejects_malformed_success_response(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond("GET", f"{ROOM_PATH}/messages", "not json")

    with pytest.raises(RuntimeError, match="invalid JSON object"):
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")


async def test_json_keeps_status_for_malformed_error_response(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond("GET", f"{ROOM_PATH}/messages", "not json", status=401)

    with pytest.raises(MatrixHTTPError) as caught:
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")

    assert caught.value.status_code == 401
    assert caught.value.errcode is None


async def test_json_rejects_response_over_size_limit(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond("GET", f"{ROOM_PATH}/messages", b"{" + b" " * (2 * 1024 * 1024))

    with pytest.raises(RuntimeError, match="size limit"):
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")


async def test_json_rejects_encoded_response_before_decompression(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        b"encoded bytes",
        headers={"Content-Encoding": "gzip"},
    )

    with pytest.raises(RuntimeError, match="content encoding"):
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")


async def test_json_retries_only_two_explicit_rate_limits(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    path = f"{ROOM_PATH}/messages"
    for _ in range(3):
        endpoint.respond(
            "POST",
            path,
            {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 0},
            status=429,
        )

    with pytest.raises(MatrixHTTPError) as caught:
        await http.json(
            "POST",
            "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages",
            body={"body": "same"},
        )

    assert caught.value.status_code == 429
    assert len(endpoint.requests) == 3
    assert [request["body"] for request in endpoint.requests] == [{"body": "same"}] * 3


async def test_json_respects_http_date_retry_after(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, endpoint = matrix_http
    path = f"{ROOM_PATH}/messages"
    endpoint.respond(
        "GET",
        path,
        {"errcode": "M_LIMIT_EXCEEDED"},
        status=429,
        headers={"Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=3))},
    )
    endpoint.respond("GET", path, {"ok": True})
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("matrix_mcp.matrix_http.asyncio.sleep", record_sleep)

    assert await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages") == {
        "ok": True
    }
    assert len(delays) == 1
    assert 0.5 < delays[0] <= 3


async def test_json_rejects_rate_limit_delay_over_five_seconds(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 5001},
        status=429,
    )

    with pytest.raises(MatrixHTTPError, match="5 seconds"):
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")

    assert len(endpoint.requests) == 1


async def test_json_rejects_extreme_integer_rate_limit_without_overflow(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 10**1000},
        status=429,
    )

    with pytest.raises(MatrixHTTPError, match="5 seconds"):
        await http.json("GET", "/_matrix/client/v3/rooms/%21room%3Aexample.com/messages")

    assert len(endpoint.requests) == 1


async def test_room_is_encrypted_treats_missing_state_as_plaintext(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/state/m.room.encryption",
        {"errcode": "M_NOT_FOUND", "error": "missing"},
        status=404,
    )

    assert await http.room_is_encrypted(ROOM) is False


async def test_room_is_encrypted_accepts_domainless_room_id(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http

    assert await http.room_is_encrypted(ROOM_V12) is False

    assert endpoint.requests[0]["path"] == (
        f"/prefix/_matrix/client/v3/rooms/{ROOM_V12}/state/m.room.encryption"
    )


async def test_room_is_encrypted_reports_encryption_state(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/state/m.room.encryption",
        {"algorithm": "m.megolm.v1.aes-sha2"},
    )

    assert await http.room_is_encrypted(ROOM) is True


async def test_room_is_encrypted_fails_closed_on_forbidden_state(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/state/m.room.encryption",
        {"errcode": "M_FORBIDDEN", "error": "denied"},
        status=403,
    )

    with pytest.raises(MatrixHTTPError) as caught:
        await http.room_is_encrypted(ROOM)

    assert caught.value.status_code == 403


@pytest.mark.parametrize(
    "room_id",
    ["", "room:example.com", "!", "!room:example.com\n", "!bad room:example.com"],
)
async def test_room_is_encrypted_rejects_malformed_room_id_before_http(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint], room_id: str
) -> None:
    http, endpoint = matrix_http

    with pytest.raises(ValueError, match="room ID"):
        await http.room_is_encrypted(room_id)

    assert endpoint.requests == []


def test_constructor_requires_credentials() -> None:
    config = MatrixMCPConfig(homeserver="https://matrix.example.com")

    with pytest.raises(RuntimeError, match="credentials"):
        MatrixHTTP(config)


@pytest.mark.parametrize("suffix", ["?tenant=example", "?", "#fragment", "#"])
def test_config_rejects_homeserver_query_and_fragment(suffix: str) -> None:
    with pytest.raises(ValueError, match=r"query|fragment"):
        MatrixMCPConfig(
            homeserver=f"https://matrix.example.com/prefix{suffix}",
            user_id="@alice:example.com",
            access_token="test-token",
        )


async def test_client_ignores_proxy_settings_from_the_environment(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, endpoint = matrix_http
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    endpoint.respond("GET", "/prefix/_matrix/client/v3/account/whoami", {"user_id": "@a:b"})

    assert await http.json("GET", "/_matrix/client/v3/account/whoami") == {"user_id": "@a:b"}


async def test_post_json_sends_no_token_unless_given(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    endpoint.respond("POST", "/prefix/_matrix/client/v3/login", {"ok": True})
    endpoint.respond("POST", "/prefix/_matrix/client/v3/logout", {})
    homeserver = http._homeserver  # noqa: SLF001 - The fixture's server URL.

    assert await post_json(homeserver, "/_matrix/client/v3/login", {"x": 1}) == {"ok": True}
    await post_json(homeserver, "/_matrix/client/v3/logout", {}, access_token="secret")

    assert "Authorization" not in endpoint.requests[0]["headers"]
    assert endpoint.requests[0]["body"] == {"x": 1}
    assert endpoint.requests[1]["headers"]["Authorization"] == "Bearer secret"


async def test_post_json_refuses_redirects_and_hides_server_error_text(
    matrix_http: tuple[MatrixHTTP, MatrixEndpoint],
) -> None:
    http, endpoint = matrix_http
    homeserver = http._homeserver  # noqa: SLF001 - The fixture's server URL.
    endpoint.respond(
        "POST", "/prefix/_matrix/client/v3/login", {}, status=307, headers={"Location": "/x"}
    )
    endpoint.respond(
        "POST",
        "/prefix/_matrix/client/v3/login",
        {"errcode": "M_FORBIDDEN", "error": "token=leaked-secret"},
        status=403,
    )

    with pytest.raises(MatrixHTTPError, match="redirect"):
        await post_json(homeserver, "/_matrix/client/v3/login", {})
    with pytest.raises(MatrixHTTPError, match=r"HTTP 403 \(M_FORBIDDEN\)") as raised:
        await post_json(homeserver, "/_matrix/client/v3/login", {})

    assert "leaked-secret" not in str(raised.value)
