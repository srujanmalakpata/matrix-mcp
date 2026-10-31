from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from matrix_mcp.auth import LoginResult
from matrix_mcp.cli import app
from matrix_mcp.config import MatrixMCPConfig

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def write_config(path: Path) -> MatrixMCPConfig:
    config = MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="TESTDEVICE",
        access_token="test-token",
    )
    config.save(path)
    return config


def test_help_lists_only_the_kept_commands() -> None:
    result = runner.invoke(app, ["--help"])
    auth = runner.invoke(app, ["auth", "--help"])

    assert result.exit_code == 0
    assert "e2ee" not in result.output
    assert {"token", "password", "logout"} <= set(auth.output.split())
    assert "sso" not in auth.output


def test_serve_takes_no_transport_options() -> None:
    result = runner.invoke(app, ["serve", "--transport", "http"])

    assert result.exit_code != 0


def test_auth_token_prompts_for_the_token_and_saves_privately(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    result = runner.invoke(
        app,
        [
            "auth",
            "token",
            "https://matrix.example.com/",
            "@alice:example.com",
            "TESTDEVICE",
            "--config",
            str(path),
        ],
        input="test-token\n",
    )

    assert result.exit_code == 0, result.output
    assert "test-token" not in result.output
    assert MatrixMCPConfig.load(path).access_token == "test-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_auth_token_rejects_an_unsafe_device_id(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    result = runner.invoke(
        app,
        [
            "auth",
            "token",
            "https://m.example.com",
            "@alice:example.com",
            "../x",
            "--config",
            str(path),
        ],
        input="test-token\n",
    )

    assert result.exit_code != 0
    assert not path.exists()


def test_auth_password_saves_login_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.json"
    seen: dict[str, Any] = {}

    async def fake_login(**kwargs: Any) -> LoginResult:
        seen.update(kwargs)
        return LoginResult(
            homeserver="https://matrix.example.com",
            user_id="@alice:example.com",
            device_id="NEWDEVICE",
            access_token="new-token",
        )

    monkeypatch.setattr("matrix_mcp.auth.login_with_password", fake_login)

    result = runner.invoke(
        app,
        ["auth", "password", "https://matrix.example.com", "alice", "--config", str(path)],
        input="hunter2\n",
    )

    assert result.exit_code == 0, result.output
    assert seen["password"] == "hunter2"
    assert MatrixMCPConfig.load(path).device_id == "NEWDEVICE"


def test_auth_logout_revokes_then_removes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.json"
    write_config(path)
    revoked: list[str | None] = []

    async def fake_logout(config: MatrixMCPConfig) -> None:
        revoked.append(config.access_token)

    monkeypatch.setattr("matrix_mcp.auth.logout", fake_logout)

    result = runner.invoke(app, ["auth", "logout", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert revoked == ["test-token"]
    assert not path.exists()


def test_auth_logout_keeps_credentials_when_revoking_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    write_config(path)

    async def failing_logout(config: MatrixMCPConfig) -> None:
        del config
        msg = "Matrix API request failed before receiving a response"
        raise RuntimeError(msg)

    monkeypatch.setattr("matrix_mcp.auth.logout", failing_logout)

    kept = runner.invoke(app, ["auth", "logout", "--config", str(path)])
    assert kept.exit_code == 1
    assert path.exists()

    forced = runner.invoke(app, ["auth", "logout", "--config", str(path), "--local-only"])
    assert forced.exit_code == 0, forced.output
    assert not path.exists()


def test_auth_logout_is_idempotent(tmp_path: Path) -> None:
    result = runner.invoke(app, ["auth", "logout", "--config", str(tmp_path / "missing.json")])

    assert result.exit_code == 0
    assert "No Matrix MCP credentials" in result.output


def test_config_path_prints_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("matrix_mcp.config.default_config_path", lambda: tmp_path / "c.json")

    result = runner.invoke(app, ["config-path"])

    assert result.output.strip() == str(tmp_path / "c.json")


@pytest.mark.parametrize(
    "arguments",
    [
        ["auth", "token", "https://m.example.com", "@a:example.com", "D", "--access-token", "x"],
        ["auth", "password", "https://m.example.com", "alice", "--password", "x"],
    ],
)
def test_secrets_cannot_be_passed_as_flags(arguments: list[str], tmp_path: Path) -> None:
    result = runner.invoke(app, [*arguments, "--config", str(tmp_path / "c.json")])

    assert result.exit_code != 0
    assert not (tmp_path / "c.json").exists()


def test_auth_password_reports_a_failed_login_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_login(**kwargs: Any) -> LoginResult:
        del kwargs
        msg = "Matrix API request failed with HTTP 403 (M_FORBIDDEN)"
        raise RuntimeError(msg)

    monkeypatch.setattr("matrix_mcp.auth.login_with_password", failing_login)

    result = runner.invoke(
        app,
        ["auth", "password", "https://m.example.com", "alice", "--config", str(tmp_path / "c")],
        input="hunter2\n",
    )

    assert result.exit_code == 1
    assert "M_FORBIDDEN" in result.output
    assert "hunter2" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_serve_runs_stdio_without_banner_dotenv_or_update_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[dict[str, Any]] = []

    class FakeServer:
        def run(self, **kwargs: Any) -> None:
            runs.append(kwargs)

    for name in ("FASTMCP_ENV_FILE", "FASTMCP_CHECK_FOR_UPDATES", "FASTMCP_TRANSPORT"):
        monkeypatch.setenv(name, "hostile")
    monkeypatch.setattr("matrix_mcp.mcp_server.create_mcp_server", FakeServer)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert runs == [{"transport": "stdio", "show_banner": False}]
    assert os.environ["FASTMCP_ENV_FILE"] == os.devnull
    assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "off"
    assert os.environ["FASTMCP_TRANSPORT"] == "stdio"
