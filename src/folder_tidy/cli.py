"""folder-tidy: sort a messy folder by rules you write. Preview first; undo any run.

    folder-tidy init                         write an example rules file (folder-tidy.toml) to edit
    folder-tidy sort FOLDER -c RULES         preview what would move (nothing is touched)
    folder-tidy sort FOLDER -c RULES --apply move the files, keeping a journal to undo with
    folder-tidy undo JOURNAL [--apply]       preview, then put every file from that run back
    folder-tidy doctor                       environment report for bug reports
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from importlib import resources
from pathlib import Path

from . import __version__
from .config import ConfigError, load
from .core import TidyError, apply_plan, apply_undo, make_plan, make_undo_plan


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="folder-tidy", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"folder-tidy {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sort", help="preview or apply sorting a folder")
    s.add_argument("folder", type=Path)
    s.add_argument("-c", "--config", type=Path, required=True, help="rules file (see `folder-tidy init`)")
    s.add_argument("--apply", action="store_true", help="actually move the files (default: preview only)")
    s.add_argument("--force", action="store_true",
                   help="allow sorting a disk's top folder, your home folder, or a software project folder")
    s.add_argument("-q", "--quiet", action="store_true", help="print only the summary")

    u = sub.add_parser("undo", help="put back every file from one run")
    u.add_argument("journal", type=Path, help="the journal path printed after the run")
    u.add_argument("--apply", action="store_true", help="actually move the files back (default: preview only)")

    i = sub.add_parser("init", help="write an example rules file to edit")
    i.add_argument("path", type=Path, nargs="?", default=Path("folder-tidy.toml"))
    i.add_argument("--overwrite", action="store_true", help="replace an existing file")

    d = sub.add_parser("doctor", help="environment report for bug reports")
    d.add_argument("--json", action="store_true")
    return p


def _print_skips(skips, label="SKIP") -> None:
    for s in skips:
        print(f"{label:<6} {s.path}  ({s.reason})")


def _sort(args) -> int:
    try:
        config = load(args.config)
        plan = make_plan(args.folder, config, force=args.force)
    except (ConfigError, TidyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not args.quiet:
        for m in plan.moves:
            print(f"MOVE   {m.src} -> {m.dst}")
        _print_skips(plan.skips)
    n, k = len(plan.moves), len(plan.skips)
    if not args.apply:
        print(f"\nPreview: {n} file(s) would move, {k} left alone. Nothing was changed.")
        if n:
            print("Run again with --apply to move them; every run can be undone.")
        return 0
    try:
        result = apply_plan(plan)
    except (TidyError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_skips(result.failed, "FAILED")
    print(f"\nMoved {len(result.moved)} file(s); {k} left alone; {len(result.failed)} failed.")
    if result.journal:
        print(f"To undo this run: folder-tidy undo \"{result.journal}\"")
    return 1 if result.failed else 0


def _undo(args) -> int:
    try:
        plan = make_undo_plan(args.journal)
    except TidyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    moves = [s for s in plan.steps if s.kind == "move"]
    for s in moves:
        print(f"BACK   {s.src} -> {s.dst}")
    _print_skips(plan.skips)
    if not args.apply:
        print(f"\nPreview: {len(moves)} file(s) would go back, {len(plan.skips)} left alone. Nothing was changed.")
        if moves:
            print("Run again with --apply to put them back.")
        return 0
    result = apply_undo(plan)
    _print_skips(result.failed, "FAILED")
    print(f"\nPut back {len(result.moved)} file(s); {len(plan.skips)} left alone; {len(result.failed)} failed.")
    return 1 if (result.failed or plan.skips) else 0


def _init(args) -> int:
    dest: Path = args.path
    if dest.exists() and not args.overwrite:
        print(f"error: {dest} already exists (use --overwrite to replace it)", file=sys.stderr)
        return 2
    text = resources.files("folder_tidy").joinpath("example.toml").read_text(encoding="utf-8")
    try:
        parent = dest.parent if str(dest.parent) else Path(".")
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{dest.name}.", suffix=".part")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        print(f"error: couldn't write {dest}: {e.strerror or e}", file=sys.stderr)
        return 2
    print(f"Wrote {dest}. Edit the rules, then preview with: folder-tidy sort FOLDER -c {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = _parser().parse_args(argv)
    if args.command == "sort":
        return _sort(args)
    if args.command == "undo":
        return _undo(args)
    if args.command == "init":
        return _init(args)
    from .doctor import run
    return run(as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
