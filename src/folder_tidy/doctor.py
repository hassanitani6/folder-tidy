"""`folder-tidy doctor`: an environment report for bug reports.

Collects only what helps reproduce a problem: tool and Python versions, OS family and release, and whether the
current folder's file system supports hard links (the safest way to move without replacing). It reads none of your
files, uploads nothing, and replaces your home folder and user name with placeholders.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import tempfile
from pathlib import Path

from . import __version__

_SECRETISH = re.compile(r"(?i)(token|secret|password|api[_-]?key)\s*[=:]\s*\S+")
_ANY_HOME = re.compile(r"(?i)(/Users/|/home/|\b[A-Z]:[\\/]+Users[\\/]+)[^/\\\s\"']+")


def _names_to_hide() -> set[str]:
    names = {Path.home().name}
    try:
        names.add(getpass.getuser())
    except (KeyError, OSError):
        pass  # no login name available (e.g. some containers); the home-folder rules still apply
    return {n for n in names if n}


def redact(text: str) -> str:
    home = str(Path.home())
    if home and home not in ("/", "\\"):
        text = re.sub(re.escape(home), "~", text, flags=re.I)
    text = _ANY_HOME.sub(lambda m: m.group(1) + "<user>", text)
    for name in _names_to_hide():
        text = re.sub(rf"(?i)(?<![\w]){re.escape(name)}(?![\w])", "<user>", text)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
    return _SECRETISH.sub(r"\1=<redacted>", text)


def _redact_values(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_values(v) for k, v in value.items()}
    return value


def _probe_current_folder() -> dict:
    """Creates two empty files in a temporary folder here, then removes them."""
    out = {}
    try:
        with tempfile.TemporaryDirectory(dir=".") as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            open(a, "w").close()
            out["case_insensitive_names"] = "yes" if os.path.exists(os.path.join(d, "A")) else "no"
            try:
                os.link(a, b)
                out["hard_links"] = "yes"
            except (OSError, NotImplementedError) as e:
                out["hard_links"] = f"no ({type(e).__name__}); moves use check-then-rename"
    except OSError as e:
        out["probe"] = f"couldn't create a temporary folder here: {type(e).__name__}"
    return out


def collect() -> dict:
    report = {
        "tool_version": __version__,
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "current_folder": _probe_current_folder(),
    }
    return _redact_values(report)


def render_text(report: dict) -> str:
    lines = ["folder-tidy doctor report", "(review before sharing — it should contain no personal details)", ""]
    for k, v in report.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            lines += [f"  {pk}: {pv}" for pk, pv in v.items()]
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def run(as_json: bool = False) -> int:
    report = collect()
    print(json.dumps(report, indent=2) if as_json else render_text(report))
    return 0
