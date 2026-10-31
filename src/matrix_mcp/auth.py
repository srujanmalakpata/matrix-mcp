from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from matrix_mcp.config import MatrixMCPConfig, validated_homeserver
from matrix_mcp.matrix_http import post_json

_USER_ID = re.compile(r"@[^\s:/]+:[^\s/?#@]+")
_DEVICE_ID = re.compile(r"[A-Za-z0-9._~+-]{1,255}")


class LoginResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    homeserver: str
    user_id: str
    device_id: str
    access_token: str = Field(repr=False)

    def to_config(self) -> MatrixMCPConfig:
        return MatrixMCPConfig(
            homeserver=self.homeserver,
            user_id=self.user_id,
            device_id=self.device_id,
            access_token=self.access_token,
        )


async def login_with_password(
    *,
    homeserver: str,
    user: str,
    password: str,
    device_name: str = "matrix-mcp",
) -> LoginResult:
    """Log in with m.login.password and create a new device for this server."""
    normalized = validated_homeserver(homeserver)
    payload = await post_json(
        normalized,
        "/_matrix/client/v3/login",
        {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
            "initial_device_display_name": device_name,
        },
    )
    return login_result(normalized, payload)


async def logout(config: MatrixMCPConfig) -> None:
    """Revoke the stored access token on the homeserver, which also deletes its device."""
    token = config.access_token_value()
    if not token:
        return
    await post_json(
        config.normalized_homeserver,
        "/_matrix/client/v3/logout",
        {},
        access_token=token,
    )


def login_result(homeserver: str, payload: dict[str, Any]) -> LoginResult:
    """Check a login response before anything from it is stored or used in a path."""
    user_id = payload.get("user_id")
    device_id = payload.get("device_id")
    access_token = payload.get("access_token")
    if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
        msg = "Matrix login response had an invalid user_id"
        raise RuntimeError(msg)
    if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
        msg = "Matrix login response had an invalid device_id"
        raise RuntimeError(msg)
    if not isinstance(access_token, str) or not access_token or _has_space(access_token):
        msg = "Matrix login response had an invalid access_token"
        raise RuntimeError(msg)
    return LoginResult(
        homeserver=homeserver,
        user_id=user_id,
        device_id=device_id,
        access_token=access_token,
    )


def _has_space(value: str) -> bool:
    return any(character.isspace() for character in value)
