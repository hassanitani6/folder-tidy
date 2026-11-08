"""Reading and checking the rules file (TOML).

Every problem in the file is collected and reported together, so one run shows everything to fix.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_CONFIG_BYTES = 1_000_000
TOP_KEYS = {"recursive", "on_conflict", "include_hidden", "rules"}
RULE_KEYS = {"to", "extensions", "names"}
CONFLICT_MODES = ("skip", "number")

# Names Windows refuses for any file or folder, with or without an extension (CON, CON.txt, ...).
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$")
_BAD_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}


def _key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    to: str  # destination folder, relative to the folder being sorted, always with "/" separators
    extensions: frozenset[str] = frozenset()  # lower-case, without the dot
    names: tuple[str, ...] = ()  # case-insensitive glob patterns on the file name

    def matches(self, filename: str) -> bool:
        name = _key(filename)  # é typed as one character or as e + accent must match the same rules
        ext = name.rsplit(".", 1)[1] if "." in name.lstrip(".") else ""
        if self.extensions and ext not in self.extensions:
            return False
        if self.names and not any(fnmatch.fnmatchcase(name, _key(p)) for p in self.names):
            return False
        return True


@dataclass(frozen=True)
class Config:
    rules: tuple[Rule, ...]
    recursive: bool = False
    on_conflict: str = "skip"
    include_hidden: bool = False
    source: Path | None = field(default=None, compare=False)


def destination_errors(to: object) -> list[str]:
    """Why a rule's `to` value is unsafe, or [] if it is fine. Destinations must stay inside the sorted folder."""
    if not isinstance(to, str) or not to.strip():
        return ["'to' must be a non-empty folder name such as \"Images\" or \"Documents/PDFs\""]
    if "\\" in to:
        return [f"'to' = {to!r}: use / between folder names, not \\"]
    p = PurePosixPath(to)
    if p.is_absolute() or re.match(r"^[A-Za-z]:", to):
        return [f"'to' = {to!r} must be relative to the folder being sorted, not an absolute path"]
    problems = []
    for part in to.split("/"):
        if part in ("..", ".", ""):
            problems.append(f"'to' = {to!r} must not contain '.', '..' or '//'; destinations stay inside the folder")
        elif part.startswith("."):
            problems.append(f"'to' = {to!r}: folder names starting with '.' are hidden and not allowed")
        elif part != part.rstrip(" .") or _RESERVED.match(part.rstrip(" .")):
            problems.append(f"'to' = {to!r}: {part!r} is not a valid folder name on Windows")
        elif any(c in _BAD_CHARS for c in part):
            problems.append(f"'to' = {to!r}: {part!r} contains a character Windows does not allow")
    return problems


def _string_list(value: object, what: str, where: str) -> tuple[list[str], list[str]]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        return [], [f"{where}: '{what}' must be a list of non-empty text values"]
    if not value:
        return [], [f"{where}: '{what}' is empty; remove it or add values"]
    return value, []


def parse(data: dict, source: Path | None = None) -> Config:
    problems = [f"unknown setting {k!r} (allowed: {', '.join(sorted(TOP_KEYS))})" for k in data if k not in TOP_KEYS]
    recursive = data.get("recursive", False)
    include_hidden = data.get("include_hidden", False)
    on_conflict = data.get("on_conflict", "skip")
    for name, val in (("recursive", recursive), ("include_hidden", include_hidden)):
        if not isinstance(val, bool):
            problems.append(f"'{name}' must be true or false")
    if on_conflict not in CONFLICT_MODES:
        problems.append(f"'on_conflict' must be one of {', '.join(CONFLICT_MODES)}")
    raw_rules = data.get("rules")
    rules: list[Rule] = []
    if not isinstance(raw_rules, list) or not raw_rules:
        problems.append("add at least one [[rules]] section with a 'to' folder and 'extensions' and/or 'names'")
        raw_rules = []
    for i, r in enumerate(raw_rules, 1):
        where = f"rule {i}"
        if not isinstance(r, dict):
            problems.append(f"{where} must be a [[rules]] table")
            continue
        problems += [f"{where}: unknown key {k!r} (allowed: {', '.join(sorted(RULE_KEYS))})" for k in r
                     if k not in RULE_KEYS]
        problems += [f"{where}: {p}" for p in destination_errors(r.get("to"))]
        exts: list[str] = []
        names: list[str] = []
        if "extensions" in r:
            exts, errs = _string_list(r["extensions"], "extensions", where)
            problems += errs
        if "names" in r:
            names, errs = _string_list(r["names"], "names", where)
            problems += errs
        if "extensions" not in r and "names" not in r:
            problems.append(f"{where}: give 'extensions' and/or 'names' so the rule knows which files to move")
        clean_exts = frozenset(_key(e.strip().lstrip(".")) for e in exts)
        if "" in clean_exts:
            problems.append(f"{where}: an extension is empty")
        if isinstance(r.get("to"), str):
            rules.append(Rule(str(PurePosixPath(r["to"])), clean_exts, tuple(n.strip() for n in names)))
    if problems:
        raise ConfigError("; ".join(problems))
    return Config(tuple(rules), recursive, on_conflict, include_hidden, source)


def load(path: Path) -> Config:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"{path.name} is larger than 1 MB; a rules file should be a few lines")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"rules file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"couldn't read rules file {path.name}: {e}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} is not valid TOML: {e}") from None
    return parse(data, path)
