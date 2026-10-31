from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError, field_validator

APP_NAME = "matrix-mcp"
_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600
_GROUP_OR_OTHER = stat.S_IRWXG | stat.S_IRWXO
_GROUP_OR_OTHER_WRITE = stat.S_IWGRP | stat.S_IWOTH
_MAX_CONFIG_BYTES = 64 * 1024
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class InsecureConfigError(RuntimeError):
    """The credentials file could be read or replaced by someone else."""


def default_config_path() -> Path:
    candidates = _default_config_path_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _default_config_path_candidates() -> tuple[Path, ...]:
    platform_path = Path(user_config_dir(APP_NAME, appauthor=False)) / "config.json"
    home_dot_config_path = Path.home() / ".config" / APP_NAME / "config.json"
    if home_dot_config_path == platform_path:
        return (platform_path,)
    return (platform_path, home_dot_config_path)


class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    homeserver: str
    user_id: str | None = None
    device_id: str | None = None
    access_token: str | None = Field(default=None, repr=False)

    @field_validator("homeserver")
    @classmethod
    def _check_homeserver(cls, value: str) -> str:
        return validated_homeserver(value)

    def access_token_value(self) -> str | None:
        return self.access_token

    def model_dump_safe(self) -> dict[str, Any]:
        dumped = self.model_dump(mode="json")
        if self.access_token_value():
            dumped["access_token"] = "<configured>"
        return dumped


class MatrixMCPConfig(AuthConfig):
    default_limit: int = 20

    @classmethod
    def load(cls, path: Path | None = None) -> MatrixMCPConfig:
        """Read the token file through one no-follow descriptor, after checking it and its
        folder."""
        resolved_path = path or default_config_path()
        uid = os.getuid()
        _refuse(resolved_path, insecure_folder_problem(resolved_path.parent.lstat(), uid=uid))
        try:
            descriptor = os.open(
                resolved_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            )
        except OSError:
            if resolved_path.is_symlink():
                _refuse(resolved_path, "it is a symlink")
            raise
        with os.fdopen(descriptor, "rb") as file:
            _refuse(resolved_path, insecure_file_problem(os.fstat(file.fileno()), uid=uid))
            raw = file.read(_MAX_CONFIG_BYTES + 1)
        return cls.from_json(raw)

    @classmethod
    def from_json(cls, raw: bytes) -> MatrixMCPConfig:
        """Parse a config, with errors that name fields but never echo their values."""
        if len(raw) > _MAX_CONFIG_BYTES:
            msg = "Invalid Matrix MCP config: the file is larger than 64 KiB"
            raise ValueError(msg)
        try:
            return cls.model_validate(json.loads(raw))
        except json.JSONDecodeError:
            msg = "Invalid Matrix MCP config: not valid JSON"
            raise ValueError(msg) from None
        except ValidationError as exc:
            fields = ", ".join(
                f"{'.'.join(map(str, error['loc'])) or 'config'}: {error['type']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
            msg = f"Invalid Matrix MCP config: {fields}"
            raise ValueError(msg) from None

    def save(self, path: Path | None = None) -> None:
        """Write the token file 0600, replacing any old file atomically.

        A missing folder is created 0700; an existing one must already be private to this user.
        """
        resolved_path = path or default_config_path()
        if resolved_path.parent.exists():
            _refuse(
                resolved_path,
                insecure_folder_problem(resolved_path.parent.lstat(), uid=os.getuid()),
            )
        else:
            resolved_path.parent.mkdir(mode=_PRIVATE_DIR, parents=True)
            resolved_path.parent.chmod(_PRIVATE_DIR)
        temporary = resolved_path.with_name(f".{resolved_path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _PRIVATE_FILE,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(self.model_dump_json(indent=2) + "\n")
            temporary.replace(resolved_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @property
    def normalized_homeserver(self) -> str:
        return str(AnyHttpUrl(self.homeserver)).rstrip("/")


def insecure_file_problem(status: os.stat_result, *, uid: int) -> str | None:
    """Why a credentials file is unsafe to trust, or None when it's a private regular file."""
    if stat.S_ISLNK(status.st_mode):
        return "it is a symlink"
    if not stat.S_ISREG(status.st_mode):
        return "it is not a regular file"
    if status.st_uid != uid:
        return "it belongs to another user"
    if status.st_mode & _GROUP_OR_OTHER:
        mode = stat.filemode(status.st_mode)
        return f"other users can access it; run chmod 600 on it (mode {mode})"
    return None


def insecure_folder_problem(status: os.stat_result, *, uid: int) -> str | None:
    """Why the token file's folder is unsafe, or None when only this user can change it."""
    if stat.S_ISLNK(status.st_mode):
        return "its folder is a symlink"
    if not stat.S_ISDIR(status.st_mode):
        return "its folder is not a directory"
    if status.st_uid != uid:
        return "its folder belongs to another user"
    if status.st_mode & _GROUP_OR_OTHER_WRITE:
        return "other users can write to its folder"
    return None


def validated_homeserver(value: str) -> str:
    """An https homeserver URL without query or fragment; plain http only for loopback or
    single-label hosts such as a container name on a private network."""
    url = AnyHttpUrl(value.strip())
    text = str(url).rstrip("/")
    if url.query is not None or url.fragment is not None or "?" in value or "#" in value:
        msg = "Matrix homeserver URL must not include a query or fragment"
        raise ValueError(msg)
    host = url.host or ""
    if url.scheme == "http" and host not in _LOOPBACK_HOSTS and "." in host:
        msg = "Matrix homeserver URL must use https (http only for localhost or a container name)"
        raise ValueError(msg)
    return text


def _refuse(path: Path, problem: str | None) -> None:
    if problem is not None:
        msg = f"Refusing to use {path}: {problem}"
        raise InsecureConfigError(msg)
