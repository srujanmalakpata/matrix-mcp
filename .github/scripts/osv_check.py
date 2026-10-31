"""Fail when any package pinned in uv.lock has a known vulnerability in OSV (osv.dev)."""

from __future__ import annotations

import json
import sys
import tomllib
import urllib.request
from pathlib import Path

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"


def locked_packages(lock: dict[str, object]) -> list[tuple[str, str]]:
    """Name and version of every registry package in a parsed uv.lock."""
    packages = lock.get("package", [])
    if not isinstance(packages, list):
        msg = "uv.lock has no package list"
        raise TypeError(msg)
    return sorted(
        (package["name"], package["version"])
        for package in packages
        if isinstance(package, dict)
        and "version" in package
        and "registry" in package.get("source", {})
    )


def osv_query(packages: list[tuple[str, str]]) -> dict[str, object]:
    return {
        "queries": [
            {"package": {"ecosystem": "PyPI", "name": name}, "version": version}
            for name, version in packages
        ]
    }


def vulnerable(
    packages: list[tuple[str, str]], response: dict[str, object]
) -> list[tuple[str, str, list[str]]]:
    """Pair each package with the OSV IDs reported for it, keeping only affected ones."""
    results = response.get("results", [])
    if not isinstance(results, list) or len(results) != len(packages):
        msg = "OSV returned a different number of results than packages queried"
        raise ValueError(msg)
    return [
        (name, version, [vuln["id"] for vuln in result.get("vulns", [])])
        for (name, version), result in zip(packages, results, strict=True)
        if result.get("vulns")
    ]


def main(lock_path: str) -> int:
    packages = locked_packages(tomllib.loads(Path(lock_path).read_text(encoding="utf-8")))
    request = urllib.request.Request(
        OSV_BATCH_URL,
        data=json.dumps(osv_query(packages)).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as reply:  # noqa: S310 - Fixed https URL.
        found = vulnerable(packages, json.load(reply))
    print(f"{len(packages)} locked packages checked against OSV, {len(found)} affected")
    for name, version, ids in found:
        print(f"  {name} {version}: {', '.join(ids)}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
