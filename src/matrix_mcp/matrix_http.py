from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from matrix_mcp.tls import default_ssl_context

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from matrix_mcp.config import MatrixMCPConfig


_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_RATE_LIMIT_DELAY_SECONDS = 5.0
RATE_LIMIT_RETRIES = 2
_CONTROL_CODE_BOUNDARY = 32
_MAX_TRANSACTION_ID_LENGTH = 255
_ERRCODE_PATTERN = re.compile(r"M_[A-Z_0-9]{1,80}")
_DELETE = 127
DEADLINE_SECONDS = 60.0
NO_RESPONSE = "Matrix API request failed before receiving a response"


class MatrixHTTPError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, errcode: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errcode = errcode


class MatrixHTTP:
    def __init__(self, config: MatrixMCPConfig) -> None:
        token = config.access_token_value()
        if not token or not config.user_id:
            msg = "Matrix credentials are incomplete. Run `matrix-mcp auth` first."
            raise RuntimeError(msg)
        self._access_token = token
        self._homeserver = config.normalized_homeserver
        if "?" in self._homeserver or "#" in self._homeserver:
            msg = "Matrix homeserver URL must not include a query or fragment"
            raise ValueError(msg)
        self.user_id = config.user_id

    def client(self) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        return http_client(
            base_url=self._homeserver,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )

    async def json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        """One request, retrying rate limits, all within DEADLINE_SECONDS."""
        if not path.startswith("/"):
            msg = "Matrix API path must start with '/'"
            raise ValueError(msg)
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                return await self._json(method, path, body=body, params=params)
        except TimeoutError:
            raise RuntimeError(NO_RESPONSE) from None

    async def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None,
        params: dict[str, str | int] | None,
    ) -> dict[str, Any]:
        url = f"{self._homeserver}{path}"
        async with self.client() as client:
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    async with client.stream(
                        method,
                        url,
                        json=body,
                        params=params,
                    ) as response:
                        if response.is_redirect:
                            msg = (
                                f"Matrix API request refused HTTP redirect ({response.status_code})"
                            )
                            raise MatrixHTTPError(msg, status_code=response.status_code)
                        response_content = await _read_bounded(response)
                except httpx.HTTPError as exc:
                    raise RuntimeError(NO_RESPONSE) from exc

                payload = _response_payload(response, response_content)
                errcode = _safe_errcode(payload.get("errcode"))
                if await retry_rate_limited(
                    response,
                    payload,
                    attempt=attempt,
                    errcode=errcode,
                ):
                    continue
                if response.is_error:
                    detail = f" ({errcode})" if errcode else ""
                    msg = f"Matrix API request failed with HTTP {response.status_code}{detail}"
                    raise MatrixHTTPError(
                        msg,
                        status_code=response.status_code,
                        errcode=errcode,
                    )
                return payload
        msg = "Matrix API request exhausted its retry budget"
        raise RuntimeError(msg)

    async def room_is_encrypted(self, room_id: str) -> bool:
        """Report room encryption; only a definitive missing state event means plaintext."""
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        path = f"/_matrix/client/v3/rooms/{room}/state/m.room.encryption"
        try:
            await self.json("GET", path)
        except MatrixHTTPError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND and exc.errcode == "M_NOT_FOUND":
                return False
            raise
        return True


async def post_json(
    homeserver: str,
    path: str,
    body: dict[str, object],
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    """POST once with MatrixHTTP.json's bounds, with or without a token (login, logout)."""
    if not path.startswith("/"):
        msg = "Matrix API path must start with '/'"
        raise ValueError(msg)
    headers = {} if access_token is None else {"Authorization": f"Bearer {access_token}"}
    async with http_client(base_url=homeserver, headers=headers) as client:
        try:
            async with (
                asyncio.timeout(DEADLINE_SECONDS),
                client.stream("POST", f"{homeserver}{path}", json=body) as response,
            ):
                if response.is_redirect:
                    msg = f"Matrix API request refused HTTP redirect ({response.status_code})"
                    raise MatrixHTTPError(msg, status_code=response.status_code)
                content = await _read_bounded(response)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RuntimeError(NO_RESPONSE) from exc
    payload = _response_payload(response, content)
    if response.is_error:
        errcode = _safe_errcode(payload.get("errcode"))
        detail = f" ({errcode})" if errcode else ""
        msg = f"Matrix API request failed with HTTP {response.status_code}{detail}"
        raise MatrixHTTPError(msg, status_code=response.status_code, errcode=errcode)
    return payload


def http_client(
    *, base_url: str, headers: dict[str, str] | None = None
) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """An HTTP client that never follows redirects or reads proxy settings from the environment."""
    return httpx.AsyncClient(
        base_url=f"{base_url}/",
        headers={"Accept": "application/json", "Accept-Encoding": "identity", **(headers or {})},
        timeout=30,
        follow_redirects=False,
        trust_env=False,
        verify=default_ssl_context(),
    )


def _unsafe_character(char: str) -> bool:
    return char.isspace() or ord(char) < _CONTROL_CODE_BOUNDARY or ord(char) == _DELETE


def quote_matrix_id(value: str, *, sigil: str, label: str) -> str:
    if (
        not value.startswith(sigil)
        or len(value) == 1
        or any(_unsafe_character(char) for char in value)
    ):
        msg = f"Invalid Matrix {label}"
        raise ValueError(msg)
    return quote(value, safe="")


def quote_transaction_id(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or len(value) > _MAX_TRANSACTION_ID_LENGTH
        or any(_unsafe_character(char) for char in value)
    ):
        msg = "Invalid Matrix transaction ID"
        raise ValueError(msg)
    return quote(value, safe="")


async def _read_bounded(response: httpx.Response) -> bytes:
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        msg = "Matrix API response used an unsupported content encoding"
        raise RuntimeError(msg)
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_JSON_BYTES:
                msg = "Matrix API response exceeded the JSON size limit"
                raise RuntimeError(msg)
        except ValueError:
            pass
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > _MAX_JSON_BYTES:
            msg = "Matrix API response exceeded the JSON size limit"
            raise RuntimeError(msg)
    return bytes(content)


def _json_object(content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "Matrix API returned an invalid JSON object"
        raise RuntimeError(msg) from exc
    if not isinstance(payload, dict):
        msg = "Matrix API returned an invalid JSON object"
        raise RuntimeError(msg)  # noqa: TRY004
    return payload


def _response_payload(response: httpx.Response, content: bytes) -> dict[str, Any]:
    try:
        return _json_object(content)
    except RuntimeError as exc:
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            return {}
        if response.is_error:
            msg = f"Matrix API request failed with HTTP {response.status_code}"
            raise MatrixHTTPError(msg, status_code=response.status_code) from exc
        raise


async def retry_rate_limited(
    response: httpx.Response,
    payload: dict[str, Any],
    *,
    attempt: int,
    errcode: str | None,
) -> bool:
    if response.status_code != HTTPStatus.TOO_MANY_REQUESTS or attempt >= RATE_LIMIT_RETRIES:
        return False
    delay = _retry_delay(response, payload)
    if delay > _MAX_RATE_LIMIT_DELAY_SECONDS:
        msg = "Matrix API rate limit requires waiting more than 5 seconds"
        raise MatrixHTTPError(
            msg,
            status_code=response.status_code,
            errcode=errcode,
        )
    await asyncio.sleep(delay)
    return True


def _retry_delay(response: httpx.Response, payload: dict[str, Any]) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    retry_after_ms = payload.get("retry_after_ms")
    if isinstance(retry_after_ms, int) and not isinstance(retry_after_ms, bool):
        if retry_after_ms <= 0:
            return 0.0
        if retry_after_ms > _MAX_RATE_LIMIT_DELAY_SECONDS * 1000:
            return _MAX_RATE_LIMIT_DELAY_SECONDS + 1
        return max(0.0, retry_after_ms / 1000)
    return 0.0


def _safe_errcode(value: object) -> str | None:
    return value if isinstance(value, str) and _ERRCODE_PATTERN.fullmatch(value) else None
