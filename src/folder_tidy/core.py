"""Planning, moving and undoing.

Safety rules, all enforced here:
- nothing is moved unless the caller asks for it (the plan is a preview);
- a file is never replaced: an existing destination, or one already planned for another file (compared
  case-insensitively and after Unicode normalisation, as macOS and Windows do), is skipped or numbered;
- every source and destination stays inside the folder being sorted, and symbolic links are never followed;
- a file that changed after the preview is left alone;
- every move is written to a journal *before* it happens, so any run can be undone.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import __version__
from .config import Config, destination_errors

JOURNAL_DIR = ".folder-tidy"
JOURNAL_FORMAT = 1
MAX_JOURNAL_BYTES = 50_000_000
NEVER_MOVE = {".ds_store", "thumbs.db", "desktop.ini"}
MAX_NUMBER = 999
# Folders that look like one item in Finder or belong to a program; moving files out of them breaks them.
PACKAGE_SUFFIXES = (".app", ".bundle", ".framework", ".plugin", ".kext", ".photoslibrary", ".musiclibrary",
                    ".tvlibrary", ".fcpbundle", ".imovielibrary", ".logicx", ".band", ".xcodeproj",
                    ".xcworkspace", ".lrdata", ".lrlibrary", ".pages", ".numbers", ".key", ".rtfd", ".sparsebundle")
PROGRAM_DIRS = {"node_modules", ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "site-packages"}
PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml",
                   "build.gradle", "Gemfile", "composer.json", "Makefile", "CMakeLists.txt")


class TidyError(Exception):
    pass


def name_key(text: str) -> str:
    """How macOS and Windows compare names: letter case ignored, and é typed two ways counts as one name."""
    return unicodedata.normalize("NFC", text).casefold()


@dataclass
class Move:
    src: str  # paths relative to the folder, with "/" separators
    dst: str
    rule: str
    size: int = -1  # what the file looked like at preview time; apply refuses it if it changed
    mtime_ns: int = -1
    ident: tuple[int, int] = (0, 0)  # (device, inode)


@dataclass
class Skip:
    path: str
    reason: str


@dataclass
class Plan:
    folder: Path
    moves: list[Move] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)


def rel(folder: Path, p: Path) -> str:
    return p.relative_to(folder).as_posix()


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _icloud_placeholder(name: str) -> str | None:
    """iCloud keeps files that aren't downloaded as '.Name.ext.icloud'; returns 'Name.ext' for those."""
    if name.startswith(".") and name.endswith(".icloud") and len(name) > len("..icloud"):
        return name[1:-len(".icloud")]
    return None


def _portable_name(name: str) -> bool:
    """Names with ':' or '\\' can't be written safely in the journal (they look like Windows drives/paths)."""
    return ":" in name or "\\" in name


def risky_folder_reason(folder: Path) -> str | None:
    """Why sorting this folder itself is risky (a root, the home folder, a program's folder), or None."""
    real = Path(os.path.realpath(folder))
    if real == Path(real.anchor) or str(real) in ("/", "\\"):
        return "it is the top of a disk"
    try:
        if real == Path(os.path.realpath(Path.home())):
            return "it is your home folder"
    except (KeyError, RuntimeError):
        pass  # no home folder is defined (some service accounts); the other checks still apply
    if os.path.ismount(real):
        return "it is the top of a drive or volume"
    if real.name.casefold().endswith(PACKAGE_SUFFIXES):
        return "it is an app or library package"
    marker = next((m for m in PROJECT_MARKERS if os.path.lexists(real / m)), None)
    if marker:
        return f"it looks like a software project (it has {marker})"
    return None


def _skip_dir_reason(dp: Path, name: str) -> str | None:
    if name.casefold().endswith(PACKAGE_SUFFIXES):
        return "app or library package; not entered (moving its contents would break it)"
    if name in PROGRAM_DIRS:
        return "program folder; not entered"
    marker = next((m for m in PROJECT_MARKERS if os.path.lexists(dp / m)), None)
    if marker:
        return f"software project (has {marker}); not entered"
    return None


def _safe_dest_dir(folder: Path, to: str) -> tuple[Path | None, str | None]:
    """The destination folder, or a reason it can't be used. Existing parts must be real folders, not links."""
    if destination_errors(to):
        return None, "unsafe destination"
    cur = folder
    for part in PurePosixPath(to).parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return None, f"{rel(folder, cur)} is a symbolic link; folder-tidy never follows links"
        if not stat.S_ISDIR(st.st_mode):
            return None, f"{rel(folder, cur)} exists and is not a folder"
    return cur, None


def _numbered(name: str, n: int) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name} ({n})"
    return f"{stem} ({n}).{ext}"


def _candidates(folder: Path, config: Config):
    """Files to consider, in a stable order, plus skips for things deliberately left alone."""
    skips: list[Skip] = []
    files: list[Path] = []
    for root, dirs, names in os.walk(folder, followlinks=False):
        root_p = Path(root)
        keep = []
        for d in sorted(dirs):
            dp = root_p / d
            if d == JOURNAL_DIR or (_is_hidden(d) and not config.include_hidden):
                continue
            if os.path.islink(dp):
                skips.append(Skip(rel(folder, dp), "symbolic link to a folder; links are never followed"))
                continue
            if config.recursive:
                why = _skip_dir_reason(dp, d)
                if why:
                    skips.append(Skip(rel(folder, dp), why))
                    continue
            keep.append(d)
        dirs[:] = keep if config.recursive else []
        for n in sorted(names):
            p = root_p / n
            original = _icloud_placeholder(n)
            if original is not None:
                skips.append(Skip(rel(folder, p.with_name(original)),
                                  "iCloud placeholder: the file isn't downloaded; open it in Finder, then re-run"))
                continue
            if n.casefold() in NEVER_MOVE or (_is_hidden(n) and not config.include_hidden):
                continue
            files.append(p)
    return files, skips


def make_plan(folder: Path, config: Config, force: bool = False) -> Plan:
    folder = Path(os.path.realpath(folder))
    if not folder.is_dir():
        raise TidyError(f"{folder} is not a folder")
    if not force:
        why = risky_folder_reason(folder)
        if why:
            raise TidyError(f"refusing to sort {folder.name or folder} because {why}; "
                            "add --force if you really mean it")
    plan = Plan(folder)
    files, plan.skips = _candidates(folder, config)
    config_file = config.source
    taken: set[str] = set()
    for p in files:
        r = rel(folder, p)
        try:
            st = os.lstat(p)
        except OSError as e:
            plan.skips.append(Skip(r, f"couldn't read: {e.strerror or e}"))
            continue
        if stat.S_ISLNK(st.st_mode):
            plan.skips.append(Skip(r, "symbolic link; links are left where they are"))
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if config_file is not None and config_file.exists() and os.path.samefile(p, config_file):
            continue  # never move the rules file itself
        rule = next((ru for ru in config.rules if ru.matches(p.name)), None)
        if rule is None:
            continue
        if _portable_name(r):
            plan.skips.append(Skip(r, "name contains ':' or '\\', which can't be undone safely; left alone"))
            continue
        dest_dir, why = _safe_dest_dir(folder, rule.to)
        if dest_dir is None:
            plan.skips.append(Skip(r, why or "unsafe destination"))
            continue
        if p.parent == dest_dir or (os.path.isdir(dest_dir) and os.path.samefile(p.parent, dest_dir)):
            continue  # already sorted
        name, n = p.name, 0
        while True:
            dst = dest_dir / name
            if not os.path.lexists(dst) and name_key(str(dst)) not in taken:
                break
            if config.on_conflict == "skip" or n >= MAX_NUMBER:
                reason = (f"{rel(folder, dst)} already exists" if os.path.lexists(dst)
                          else f"another file is already going to {rel(folder, dst)}")
                plan.skips.append(Skip(r, reason + " (nothing is ever replaced)"))
                dst = None
                break
            n += 1
            name = _numbered(p.name, n)
        if dst is None:
            continue
        taken.add(name_key(str(dst)))
        plan.moves.append(Move(r, rel(folder, dst), rule.to, st.st_size, st.st_mtime_ns, (st.st_dev, st.st_ino)))
    return plan


# ── moving ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def move_no_clobber(src: Path, dst: Path) -> None:
    """Move a file without ever replacing dst. Raises FileExistsError if dst exists.

    A hard link fails atomically when the name is taken, so it is tried first; file systems without hard links
    fall back to check-then-rename (os.rename refuses to replace on Windows; the window on other systems is tiny).
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        # No hard links here (FAT/exFAT drives, some network shares, cross-device): check, then rename.
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "destination exists", str(dst)) from None
        os.rename(src, dst)
        return
    # Before removing the old name, make sure it is still the file we just linked: if something swapped another
    # file in between, unlinking would delete that file instead.
    try:
        same = _same_file(os.lstat(src), os.lstat(dst))
    except OSError:
        same = False
    if not same:
        os.unlink(dst)
        raise OSError(errno.EBUSY, "the file was replaced while it was being moved; left as it was")
    try:
        os.unlink(src)
    except OSError:
        os.unlink(dst)  # leave things as they were rather than with two names for one file
        raise


def _unchanged(p: Path, size: int, mtime_ns: int) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size == size and st.st_mtime_ns == mtime_ns


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self._f = open(path, "x", encoding="utf-8", newline="\n")

    def write(self, record: dict) -> None:
        self._f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()


def new_journal(folder: Path) -> Journal:
    jdir = folder / JOURNAL_DIR
    if os.path.islink(jdir):
        raise TidyError(f"{JOURNAL_DIR} is a symbolic link; remove it so the undo journal stays inside the folder")
    jdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in range(100):
        path = jdir / (f"run-{stamp}.jsonl" if i == 0 else f"run-{stamp}-{i}.jsonl")
        try:
            return Journal(path)
        except FileExistsError:
            continue
    raise TidyError("couldn't create a journal file name")


@dataclass
class Result:
    moved: list[Move] = field(default_factory=list)
    failed: list[Skip] = field(default_factory=list)
    journal: Path | None = None


def _make_dirs(folder: Path, to: str, journal: Journal) -> None:
    """Create a destination folder one level at a time, journaling each folder before creating it."""
    cur = folder
    for part in PurePosixPath(to).parts:
        cur = cur / part
        if os.path.lexists(cur):
            continue
        journal.write({"op": "mkdir", "path": rel(folder, cur)})
        cur.mkdir()


def apply_plan(plan: Plan) -> Result:
    folder = plan.folder
    result = Result()
    if not plan.moves:
        return result
    journal = new_journal(folder)
    result.journal = journal.path

    def fail(m: Move, reason: str, journaled: bool) -> None:
        result.failed.append(Skip(m.src, reason))
        if journaled:
            journal.write({"op": "failed", "src": m.src, "dst": m.dst})

    try:
        journal.write({"format": JOURNAL_FORMAT, "tool": f"folder-tidy {__version__}",
                       "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
        for m in plan.moves:
            src, dst = folder / m.src, folder / m.dst
            try:
                st = os.lstat(src)
            except OSError:
                fail(m, "it disappeared after the preview", False)
                continue
            if not stat.S_ISREG(st.st_mode) or (st.st_size, st.st_mtime_ns) != (m.size, m.mtime_ns) \
                    or (st.st_dev, st.st_ino) != tuple(m.ident):
                fail(m, "it changed after the preview; left alone (preview again)", False)
                continue
            to = PurePosixPath(m.dst).parent.as_posix()
            dest_dir, why = _safe_dest_dir(folder, to)
            if dest_dir is None:
                fail(m, why or "unsafe destination", False)
                continue
            if os.path.lexists(dst):
                fail(m, f"{m.dst} appeared after the preview; nothing was replaced", False)
                continue
            try:
                _make_dirs(folder, to, journal)
            except OSError as e:
                fail(m, f"couldn't create {to}: {e.strerror or e}", False)
                continue
            journal.write({"op": "move", "src": m.src, "dst": m.dst, "size": st.st_size,
                           "mtime_ns": st.st_mtime_ns})
            try:
                move_no_clobber(src, dst)
            except FileExistsError:
                fail(m, f"{m.dst} appeared after the preview; nothing was replaced", True)
                continue
            except OSError as e:
                fail(m, f"couldn't move: {e.strerror or e}", True)
                continue
            result.moved.append(m)
    finally:
        journal.close()
    return result


# ── undo ────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class UndoStep:
    kind: str  # "move" or "rmdir"
    src: str  # for a move: where the file is now; for rmdir: the folder
    dst: str = ""  # for a move: where it goes back to


@dataclass
class UndoPlan:
    folder: Path
    journal: Path
    steps: list[UndoStep] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)


def _inside(folder: Path, value: object) -> str | None:
    """A journal path, checked: relative, no '..', no drive or share, no links on the way. None if unsafe."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return None
    win = PureWindowsPath(value)
    if win.drive or win.anchor:
        return None
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("..", ".", "") for part in value.split("/")) \
            or value.split("/")[0] == JOURNAL_DIR:
        return None
    cur = folder
    for part in p.parts[:-1]:
        cur = cur / part
        if os.path.islink(cur):
            return None
    return p.as_posix()


def undone_marker(journal: Path) -> Path:
    return journal.with_name(journal.name + ".undone")


def read_journal(journal: Path) -> tuple[Path, list[dict]]:
    journal = Path(os.path.abspath(journal))
    if journal.parent.name != JOURNAL_DIR:
        raise TidyError(f"{journal.name} is not inside a {JOURNAL_DIR} folder; give the journal path that "
                        "folder-tidy printed after the run")
    if os.path.islink(journal) or os.path.islink(journal.parent):
        raise TidyError("the journal or its folder is a symbolic link; refusing to use it")
    try:
        if journal.stat().st_size > MAX_JOURNAL_BYTES:
            raise TidyError("the journal is larger than 50 MB, which no real run produces")
        lines = journal.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise TidyError(f"journal not found: {journal}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise TidyError(f"couldn't read the journal: {e}") from None
    records = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            raise TidyError(f"journal line {i} is damaged") from None
        if not isinstance(rec, dict):
            raise TidyError(f"journal line {i} is damaged")
        records.append(rec)
    if not records or records[0].get("format") != JOURNAL_FORMAT:
        raise TidyError("this isn't a folder-tidy journal, or it was made by an incompatible version")
    return Path(os.path.realpath(journal.parent.parent)), records[1:]


def make_undo_plan(journal: Path) -> UndoPlan:
    folder, records = read_journal(journal)
    plan = UndoPlan(folder, Path(os.path.abspath(journal)))
    if os.path.exists(undone_marker(plan.journal)):
        raise TidyError("this run was already undone")
    failed = {(r.get("src"), r.get("dst")) for r in records if r.get("op") == "failed"}
    going_back: set[str] = set()
    for rec in reversed(records):
        op = rec.get("op")
        if op == "failed":
            continue
        if op == "move":
            if (rec.get("src"), rec.get("dst")) in failed:
                continue  # journaled, then the move failed: nothing to put back
            src = _inside(folder, rec.get("src"))
            dst = _inside(folder, rec.get("dst"))
            size, mtime = rec.get("size"), rec.get("mtime_ns")
            if src is None or dst is None or not isinstance(size, int) or not isinstance(mtime, int):
                plan.skips.append(Skip(str(rec.get("dst")), "damaged or unsafe journal entry; ignored"))
                continue
            if not os.path.lexists(folder / dst) and os.path.lexists(folder / src):
                continue  # the move never happened (the run was interrupted); nothing to undo
            if not _unchanged(folder / dst, size, mtime):
                plan.skips.append(Skip(dst, "missing or changed since it was moved; left alone"))
                continue
            key = name_key(str(folder / src))
            if os.path.lexists(folder / src) or key in going_back:
                plan.skips.append(Skip(dst, f"{src} is taken by another file; left alone (nothing is replaced)"))
                continue
            going_back.add(key)
            plan.steps.append(UndoStep("move", dst, src))
        elif op == "mkdir":
            d = _inside(folder, rec.get("path"))
            if d is None:
                plan.skips.append(Skip(str(rec.get("path")), "damaged or unsafe journal entry; ignored"))
                continue
            plan.steps.append(UndoStep("rmdir", d))
        else:
            plan.skips.append(Skip("?", f"unknown journal entry {op!r}; ignored"))
    return plan


def apply_undo(plan: UndoPlan) -> Result:
    folder = plan.folder
    result = Result(journal=plan.journal)
    for step in plan.steps:
        if step.kind == "move":
            src, dst = folder / step.src, folder / step.dst
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if os.path.islink(dst.parent):
                    raise OSError(errno.ELOOP, "folder is a symbolic link")
                move_no_clobber(src, dst)
                result.moved.append(Move(step.src, step.dst, "undo"))
            except FileExistsError:
                result.failed.append(Skip(step.src, f"{step.dst} appeared meanwhile; nothing was replaced"))
            except OSError as e:
                result.failed.append(Skip(step.src, f"couldn't move back: {e.strerror or e}"))
        else:
            p = folder / step.src
            if os.path.islink(p) or not p.is_dir():
                continue
            leftovers = [n for n in os.listdir(p) if n.casefold() != ".ds_store"]
            if leftovers:
                result.failed.append(Skip(step.src, "folder not removed: it still has files"))
                continue
            try:
                for n in os.listdir(p):
                    os.unlink(p / n)
                p.rmdir()
            except OSError as e:
                result.failed.append(Skip(step.src, f"couldn't remove folder: {e.strerror or e}"))
    if not result.failed and not plan.skips:
        undone_marker(plan.journal).write_text("undone " + time.strftime("%Y-%m-%dT%H:%M:%S") + "\n",
                                               encoding="utf-8")
    return result
