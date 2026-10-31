from __future__ import annotations

import json
import subprocess
import sys


def test_cli_help_does_not_import_runtime_dependencies() -> None:
    script = """
import json
import sys

from typer.testing import CliRunner

from matrix_mcp.cli import app

result = CliRunner().invoke(app, ["--help"])
loaded = {
    name: name in sys.modules
    for name in ["fastmcp", "httpx", "platformdirs", "pydantic"]
}
print(json.dumps({"exit_code": result.exit_code, "loaded": loaded}))
"""

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["exit_code"] == 0
    assert payload["loaded"] == {
        "fastmcp": False,
        "httpx": False,
        "platformdirs": False,
        "pydantic": False,
    }
