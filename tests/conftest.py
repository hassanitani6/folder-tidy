"""Shared helpers. Every test builds its own small synthetic folder in pytest's temporary directory."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

BASIC_RULES = """
[[rules]]
to = "Images/Screenshots"
names = ["Screenshot*"]

[[rules]]
to = "Images"
extensions = ["jpg", "png"]

[[rules]]
to = "Documents"
extensions = ["pdf", "txt"]
"""


def make_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    """Every file under root (relative path -> content), ignoring the journal folder."""
    out = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".folder-tidy"]
        for n in names:
            p = Path(dirpath) / n
            out[p.relative_to(root).as_posix()] = p.read_bytes().decode("utf-8", "replace")
    return out


def all_dirs(root: Path) -> set[str]:
    return {Path(d).relative_to(root).as_posix() for d, _, _ in os.walk(root)} - {"."}


@pytest.fixture
def rules_file(tmp_path: Path):
    def write(text: str = BASIC_RULES, name: str = "rules.toml", where: Path | None = None) -> Path:
        p = (where or tmp_path) / name
        p.write_text(text, encoding="utf-8")
        return p
    return write


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    f = tmp_path / "messy"
    f.mkdir()
    return f


def can_symlink(tmp_path: Path) -> bool:
    try:
        os.symlink(tmp_path, tmp_path / "_probe_link")
        os.unlink(tmp_path / "_probe_link")
        return True
    except (OSError, NotImplementedError):
        return False
