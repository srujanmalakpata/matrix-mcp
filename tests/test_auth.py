from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.auth import login_result, login_with_password, logout
from matrix_mcp.config import MatrixMCPConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

LOGIN = {"user_id": "@alice:example.com", "device_id": "NEWDEVICE", "access_token": "tok"}


@dataclass
class AuthEndpoint:
    responses: dict[str, tuple[object, int]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "path": request.path,
                "headers": dict(request.headers),
                "body": await request.json() if request.can_read_body else None,
            }
        )
        data, status = self.responses.get(request.path, ({"errcode": "M_NOT_FOUND"}, 404))
        return web.json_response(data, status=status)


@pytest.fixture
async def homeserver() -> AsyncIterator[tuple[str, AuthEndpoint]]:
    endpoint = AuthEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/"), endpoint


async def test_password_login_posts_m_login_password_and_returns_the_device(
    homeserver: tuple[str, AuthEndpoint],
) -> None:
    url, endpoint = homeserver
    endpoint.responses["/_matrix/client/v3/login"] = (LOGIN, 200)

    result = await login_with_password(
        homeserver=f"{url}/", user="alice", password="hunter2", device_name="hub"
    )

    assert (result.homeserver, result.user_id, result.device_id) == (
        url,
        "@alice:example.com",
        "NEWDEVICE",
    )
    assert endpoint.requests[0]["body"] == {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": "alice"},
        "password": "hunter2",
        "initial_device_display_name": "hub",
    }
    assert "Authorization" not in endpoint.requests[0]["headers"]
    assert "tok" not in repr(result)


async def test_password_login_failure_hides_server_error_text(
    homeserver: tuple[str, AuthEndpoint],
) -> None:
    url, endpoint = homeserver
    endpoint.responses["/_matrix/client/v3/login"] = (
        {"errcode": "M_FORBIDDEN", "error": "password hunter2 is wrong"},
        403,
    )

    with pytest.raises(RuntimeError, match="M_FORBIDDEN") as raised:
        await login_with_password(homeserver=url, user="alice", password="hunter2")

    assert "hunter2" not in str(raised.value)


async def test_logout_revokes_the_stored_token(homeserver: tuple[str, AuthEndpoint]) -> None:
    url, endpoint = homeserver
    endpoint.responses["/_matrix/client/v3/logout"] = ({}, 200)
    config = MatrixMCPConfig(
        homeserver=url, user_id="@alice:example.com", device_id="D", access_token="tok"
    )

    await logout(config)

    assert endpoint.requests[0]["path"] == "/_matrix/client/v3/logout"
    assert endpoint.requests[0]["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "/etc/passwd"},
        {"user_id": "@alice:example.com/../x"},
        {"user_id": None},
        {"device_id": "../outside"},
        {"device_id": ""},
        {"access_token": "two words"},
        {"access_token": 7},
    ],
)
def test_login_result_rejects_unsafe_fields(change: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="invalid"):
        login_result("https://m.example.com", {**LOGIN, **change})
