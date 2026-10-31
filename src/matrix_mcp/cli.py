from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from matrix_mcp.config import MatrixMCPConfig

# Never print local variables (passwords, tokens) in tracebacks, whatever Typer's default is.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
auth_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
app.add_typer(auth_app, name="auth")


@app.command()
def serve() -> None:
    """Run the MCP server over stdio."""
    # FastMCP reads these when it is imported: no .env file from the working directory, no PyPI
    # update check, no banner, stdio only.
    os.environ["FASTMCP_ENV_FILE"] = os.devnull
    os.environ["FASTMCP_CHECK_FOR_UPDATES"] = "off"
    os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
    os.environ["FASTMCP_TRANSPORT"] = "stdio"
    from matrix_mcp.mcp_server import create_mcp_server

    create_mcp_server().run(transport="stdio", show_banner=False)


@app.command()
def config_path() -> None:
    """Print the path to the Matrix MCP config file."""
    typer.echo(str(_resolve_config_path(None)))


@auth_app.command("token")
def auth_token(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    user_id: str = typer.Argument(..., help="Matrix user ID, e.g. @alice:example.com"),
    device_id: str = typer.Argument(..., help="Matrix device ID the token belongs to"),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Store an existing Matrix access token, which is prompted for and never echoed."""
    from matrix_mcp.auth import login_result
    from matrix_mcp.config import validated_homeserver

    config_path = _resolve_config_path(config)
    access_token = typer.prompt("Access token", hide_input=True)
    try:
        result = login_result(
            validated_homeserver(homeserver),
            {"user_id": user_id, "device_id": device_id, "access_token": access_token},
        )
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    result.to_config().save(config_path)
    typer.echo(f"Saved Matrix MCP credentials for {result.user_id} to {config_path}")


@auth_app.command("password")
def auth_password(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    user: str = typer.Argument(..., help="Matrix user ID or localpart"),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Log in with a password (prompted, never echoed) and store the resulting access token."""
    from matrix_mcp.auth import login_with_password

    config_path = _resolve_config_path(config)
    password = typer.prompt("Password", hide_input=True)
    try:
        result = asyncio.run(
            login_with_password(
                homeserver=homeserver,
                user=user,
                password=password,
                device_name=device_name,
            ),
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"Login failed: {exc}", err=True)
        raise typer.Exit(1) from None
    result.to_config().save(config_path)
    typer.echo(f"Saved Matrix MCP credentials for {result.user_id} to {config_path}")


@auth_app.command("logout")
def auth_logout(
    config: Path | None = typer.Option(None, "--config", help="Config file to remove"),
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="Delete the local file even if the homeserver can't revoke the token",
    ),
) -> None:
    """Revoke the stored token on the homeserver, then remove the local credentials."""
    from matrix_mcp.auth import logout

    config_path = _resolve_config_path(config)
    if not config_path.exists():
        typer.echo(f"No Matrix MCP credentials found at {config_path}")
        return
    saved = _load_credentials(config_path)
    try:
        asyncio.run(logout(saved))
        typer.echo("Revoked the access token on the homeserver")
    except (RuntimeError, ValueError) as exc:
        if not local_only:
            typer.echo(
                f"Could not revoke the token ({exc}); nothing was removed. "
                "Retry, or pass --local-only and delete the device in another client.",
                err=True,
            )
            raise typer.Exit(1) from exc
        typer.echo(f"Could not revoke the token ({exc}); delete the device in another client")
    config_path.unlink()
    typer.echo(f"Removed Matrix MCP credentials from {config_path}")


def _load_credentials(config_path: Path) -> MatrixMCPConfig:
    from pydantic import ValidationError

    from matrix_mcp.config import MatrixMCPConfig

    try:
        return MatrixMCPConfig.load(config_path)
    except (OSError, ValueError, RuntimeError, ValidationError) as exc:
        typer.echo(f"Cannot read Matrix MCP credentials from {config_path}: {exc}", err=True)
        raise typer.Exit(1) from exc


def _resolve_config_path(config: Path | None) -> Path:
    if config is not None:
        return config
    from matrix_mcp.config import default_config_path

    return default_config_path()
