from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from matrix_mcp.config import (
    AuthConfig,
    InsecureConfigError,
    MatrixMCPConfig,
    insecure_file_problem,
)

if TYPE_CHECKING:
    from pathlib import Path


def sample() -> MatrixMCPConfig:
    return MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
    )


def test_config_round_trips_token_auth(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    sample().save(path)

    assert MatrixMCPConfig.load(path) == sample()


def test_save_writes_a_private_file_in_a_private_folder(tmp_path: Path) -> None:
    path = tmp_path / "matrix-mcp" / "config.json"
    old_umask = os.umask(0o022)
    try:
        sample().save(path)
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [entry.name for entry in path.parent.iterdir()] == ["config.json"]


def test_save_replaces_a_symlink_instead_of_writing_through_it(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("untouched", encoding="utf-8")
    path = tmp_path / "config.json"
    path.symlink_to(target)

    sample().save(path)

    assert target.read_text(encoding="utf-8") == "untouched"
    assert not path.is_symlink()


def test_load_refuses_a_file_other_users_can_read(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    sample().save(path)
    path.chmod(0o644)

    with pytest.raises(InsecureConfigError, match="chmod 600"):
        MatrixMCPConfig.load(path)


def test_load_refuses_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    sample().save(real)
    link = tmp_path / "config.json"
    link.symlink_to(real)

    with pytest.raises(InsecureConfigError, match="symlink"):
        MatrixMCPConfig.load(link)


def test_insecure_file_problem_checks_owner_type_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    sample().save(path)
    status = path.lstat()

    assert insecure_file_problem(status, uid=status.st_uid) is None
    assert insecure_file_problem(status, uid=status.st_uid + 1) == "it belongs to another user"
    assert insecure_file_problem(tmp_path.lstat(), uid=status.st_uid) == "it is not a regular file"


def test_config_rejects_unknown_fields_such_as_old_header_commands() -> None:
    with pytest.raises(ValueError, match="http_header_commands"):
        MatrixMCPConfig.model_validate(
            {"homeserver": "https://m.example.com", "http_header_commands": {"X": "sh"}}
        )


def test_default_config_loads_existing_home_dot_config_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_config_dir = tmp_path / "Library" / "Application Support" / "matrix-mcp"
    fallback_config = tmp_path / ".config" / "matrix-mcp" / "config.json"
    sample().save(fallback_config)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def fake_user_config_dir(_appname: str, *, appauthor: bool) -> str:
        assert appauthor is False
        return str(platform_config_dir)

    monkeypatch.setattr("matrix_mcp.config.user_config_dir", fake_user_config_dir)

    assert MatrixMCPConfig.load().user_id == "@alice:example.com"


def test_auth_config_redacts_access_token() -> None:
    config = AuthConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
    )

    dumped = config.model_dump_safe()

    assert dumped["access_token"] == "<configured>"
    assert dumped["user_id"] == "@alice:example.com"
    assert "test-token" not in repr(config)


def test_invalid_config_errors_name_fields_but_never_echo_values() -> None:
    raw = (
        b'{"homeserver": "https://m.example.com", "access_token": "TOKEN_SECRET",'
        b' "http_headers": {"X-Secret": "HEADER_SECRET"}}'
    )

    with pytest.raises(ValueError, match="http_headers: extra_forbidden") as raised:
        MatrixMCPConfig.from_json(raw)

    assert "SECRET" not in str(raised.value)
    assert raised.value.__suppress_context__ is True


def test_load_refuses_oversized_and_non_json_files(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    sample().save(path)
    path.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="64 KiB"):
        MatrixMCPConfig.load(path)

    path.write_bytes(b"{not json TOKEN_SECRET")
    with pytest.raises(ValueError, match="not valid JSON") as raised:
        MatrixMCPConfig.load(path)
    assert "TOKEN_SECRET" not in str(raised.value)


def test_load_and_save_refuse_a_folder_others_can_write(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    path = folder / "config.json"
    sample().save(path)
    folder.chmod(0o777)

    with pytest.raises(InsecureConfigError, match="write to its folder"):
        MatrixMCPConfig.load(path)
    with pytest.raises(InsecureConfigError, match="write to its folder"):
        sample().save(path)


def test_save_leaves_an_existing_private_folder_mode_alone(tmp_path: Path) -> None:
    folder = tmp_path / "home"
    folder.mkdir(mode=0o750)
    folder.chmod(0o750)

    sample().save(folder / "m.json")

    assert stat.S_IMODE(folder.stat().st_mode) == 0o750


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://hub.tail1234.ts.net/", "https://hub.tail1234.ts.net"),
        ("http://127.0.0.1:8008", "http://127.0.0.1:8008"),
        ("http://synapse:8008", "http://synapse:8008"),
    ],
)
def test_homeserver_accepts_https_loopback_and_container_names(url: str, expected: str) -> None:
    assert MatrixMCPConfig(homeserver=url).homeserver == expected


@pytest.mark.parametrize(
    "url", ["http://matrix.example.com", "https://m.example.com/?x=1", "ftp://m.example.com"]
)
def test_homeserver_rejects_plain_http_to_named_hosts_and_queries(url: str) -> None:
    with pytest.raises(ValueError, match=r"homeserver|URL"):
        MatrixMCPConfig(homeserver=url)
