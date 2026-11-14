#!/usr/bin/env python3
"""Bump the project patch version and keep metadata in sync."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
PYPROJECT_FILE = ROOT / "pyproject.toml"


def _read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise ValueError(f"Invalid VERSION value: {text!r}")
    return text


def _bump_version(version: str) -> str:
    major, minor, patch = map(int, version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _update_file(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated = re.sub(pattern, replacement, text, count=1)
    if updated == text:
        raise ValueError(f"Pattern not found in {path}")
    path.write_text(updated, encoding="utf-8")


def main() -> None:
    old_version = _read_version()
    new_version = _bump_version(old_version)

    VERSION_FILE.write_text(f"{new_version}\n", encoding="utf-8")
    _update_file(
        PYPROJECT_FILE,
        r'(?m)^version\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"',
        f'version = "{new_version}"',
    )
    print(f"Version bumped from {old_version} to {new_version}")


if __name__ == "__main__":
    main()
