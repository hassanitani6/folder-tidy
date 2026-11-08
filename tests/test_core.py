import json
import os
from pathlib import Path

import pytest

from conftest import BASIC_RULES, all_dirs, can_symlink, make_tree, snapshot
from folder_tidy import core
from folder_tidy.config import load
from folder_tidy.core import apply_plan, apply_undo, make_plan, make_undo_plan, move_no_clobber


def _cfg(rules_file, **top):
    head = "".join(f"{k} = {json.dumps(v)}\n" for k, v in top.items())  # JSON true/false/"text" is valid TOML
    return load(rules_file(head + BASIC_RULES))


def test_preview_changes_nothing(folder, rules_file):
    make_tree(folder, {"a.jpg": "1", "b.pdf": "2", "Screenshot 1.png": "3", "notes.xyz": "4"})
    before, dirs = snapshot(folder), all_dirs(folder)
    plan = make_plan(folder, _cfg(rules_file))
    assert [(m.src, m.dst) for m in plan.moves] == [
        ("Screenshot 1.png", "Images/Screenshots/Screenshot 1.png"), ("a.jpg", "Images/a.jpg"),
        ("b.pdf", "Documents/b.pdf")]
    assert snapshot(folder) == before and all_dirs(folder) == dirs
    assert not (folder / ".folder-tidy").exists()


def test_apply_then_undo_restores_everything(folder, rules_file):
    make_tree(folder, {"a.jpg": "1", "B.PNG": "2", "c.txt": "3", "Screenshot x.png": "4", "keep.xyz": "5"})
    before, dirs = snapshot(folder), all_dirs(folder)
    result = apply_plan(make_plan(folder, _cfg(rules_file)))
    assert len(result.moved) == 4 and not result.failed
    assert snapshot(folder) == {"Images/a.jpg": "1", "Images/B.PNG": "2", "Documents/c.txt": "3",
                                "Images/Screenshots/Screenshot x.png": "4", "keep.xyz": "5"}
    undo = make_undo_plan(result.journal)
    assert not undo.skips
    res = apply_undo(undo)
    assert not res.failed
    assert snapshot(folder) == before
    assert all_dirs(folder) - {".folder-tidy"} == dirs  # the folders it created are gone again
    with pytest.raises(core.TidyError, match="already undone"):
        make_undo_plan(result.journal)


def test_journal_holds_only_relative_paths(folder, rules_file):
    make_tree(folder, {"a.jpg": "1"})
    result = apply_plan(make_plan(folder, _cfg(rules_file)))
    text = result.journal.read_text(encoding="utf-8")
    assert str(folder) not in text and str(Path.home()) not in text
    recs = [json.loads(line) for line in text.splitlines()]
    assert recs[0]["format"] == 1
    assert {"op": "move", "src": "a.jpg", "dst": "Images/a.jpg"}.items() <= recs[-1].items()


def test_existing_destination_is_never_replaced(folder, rules_file):
    make_tree(folder, {"a.jpg": "new", "Images/a.jpg": "old"})
    plan = make_plan(folder, _cfg(rules_file))
    assert not plan.moves and "already exists" in plan.skips[0].reason
    apply_plan(plan)
    assert snapshot(folder) == {"a.jpg": "new", "Images/a.jpg": "old"}


def test_number_mode_keeps_both(folder, rules_file):
    make_tree(folder, {"a.jpg": "new", "Images/a.jpg": "old", "Images/a (1).jpg": "older"})
    cfg = _cfg(rules_file, on_conflict="number")
    apply_plan(make_plan(folder, cfg))
    assert snapshot(folder) == {"Images/a.jpg": "old", "Images/a (1).jpg": "older", "Images/a (2).jpg": "new"}


def test_names_differing_only_in_case_never_collide(folder, rules_file, tmp_path):
    # Two sources that would land on names differing only in letter case: on macOS/Windows that is one file.
    make_tree(folder, {"x/photo.jpg": "1", "y/PHOTO.JPG": "2"})
    cfg = _cfg(rules_file, recursive=True)
    plan = make_plan(folder, cfg)
    assert len(plan.moves) == 1 and "another file is already going to" in plan.skips[0].reason
    cfg_n = _cfg(rules_file, recursive=True, on_conflict="number")
    moves = make_plan(folder, cfg_n).moves
    assert sorted(m.dst for m in moves) == ["Images/PHOTO (1).JPG", "Images/photo.jpg"]


def test_destination_appearing_after_preview_is_not_replaced(folder, rules_file):
    make_tree(folder, {"a.jpg": "mine"})
    plan = make_plan(folder, _cfg(rules_file))
    make_tree(folder, {"Images/a.jpg": "someone else's"})
    result = apply_plan(plan)
    assert not result.moved and "appeared after the preview" in result.failed[0].reason
    assert snapshot(folder) == {"a.jpg": "mine", "Images/a.jpg": "someone else's"}


def test_move_without_hard_links_still_refuses_to_replace(tmp_path, monkeypatch):
    def no_links(*a, **k):
        raise PermissionError(1, "hard links not supported")
    monkeypatch.setattr(core.os, "link", no_links)
    make_tree(tmp_path, {"a": "1", "b": "2", "c": "3"})
    with pytest.raises(FileExistsError):
        move_no_clobber(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "b").read_text() == "2" and (tmp_path / "a").exists()
    move_no_clobber(tmp_path / "c", tmp_path / "d")
    assert (tmp_path / "d").read_text() == "3" and not (tmp_path / "c").exists()


def test_move_with_hard_links_refuses_to_replace(tmp_path):
    make_tree(tmp_path, {"a": "1", "b": "2"})
    with pytest.raises(FileExistsError):
        move_no_clobber(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "a").read_text() == "1" and (tmp_path / "b").read_text() == "2"


def test_hidden_system_and_rules_files_are_left_alone(folder, rules_file):
    make_tree(folder, {".secret.txt": "1", ".DS_Store": "2", "Thumbs.db": "3", "a.txt": "4"})
    rules = rules_file(where=folder, name="rules.txt.toml")
    rules_txt = folder / "my-rules.txt"
    rules_txt.write_text(rules.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load(rules_txt)  # a rules file whose name matches a rule (.txt) must not move itself
    plan = make_plan(folder, cfg)
    assert [m.src for m in plan.moves] == ["a.txt"]


def test_include_hidden(folder, rules_file):
    make_tree(folder, {".x.txt": "1", ".DS_Store": "2"})
    plan = make_plan(folder, _cfg(rules_file, include_hidden=True))
    assert [m.src for m in plan.moves] == [".x.txt"]


def test_icloud_placeholders_are_reported(folder, rules_file):
    make_tree(folder, {".photo.jpg.icloud": "stub"})
    plan = make_plan(folder, _cfg(rules_file))
    assert not plan.moves and plan.skips[0].path == "photo.jpg" and "iCloud" in plan.skips[0].reason


def test_recursive_skips_already_sorted_and_journal(folder, rules_file):
    make_tree(folder, {"Images/a.jpg": "1", "sub/b.jpg": "2", ".folder-tidy/run-x.jsonl": "{}"})
    plan = make_plan(folder, _cfg(rules_file, recursive=True))
    assert [(m.src, m.dst) for m in plan.moves] == [("sub/b.jpg", "Images/b.jpg")]


def test_not_recursive_by_default(folder, rules_file):
    make_tree(folder, {"sub/b.jpg": "2"})
    assert not make_plan(folder, _cfg(rules_file)).moves


def test_symlinked_files_and_folders_are_not_followed(folder, rules_file, tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("this system doesn't allow creating symbolic links")
    outside = tmp_path / "outside"
    make_tree(outside, {"o.jpg": "outside"})
    os.symlink(outside / "o.jpg", folder / "link.jpg")
    os.symlink(outside, folder / "linkdir", target_is_directory=True)
    plan = make_plan(folder, _cfg(rules_file, recursive=True))
    assert not plan.moves
    assert {s.path for s in plan.skips} == {"link.jpg", "linkdir"}


def test_destination_folder_that_is_a_link_is_refused(folder, rules_file, tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("this system doesn't allow creating symbolic links")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, folder / "Images", target_is_directory=True)
    make_tree(folder, {"a.jpg": "1"})
    plan = make_plan(folder, _cfg(rules_file))
    reasons = {s.path: s.reason for s in plan.skips}
    assert not plan.moves and "Images is a symbolic link" in reasons["a.jpg"]
    apply_plan(plan)
    assert list(outside.iterdir()) == []


def test_destination_path_through_a_file_is_refused(folder, rules_file):
    make_tree(folder, {"Images": "I am a file", "a.jpg": "1"})
    plan = make_plan(folder, _cfg(rules_file))
    assert not plan.moves and "not a folder" in plan.skips[0].reason


def test_undo_leaves_changed_files_alone(folder, rules_file):
    make_tree(folder, {"a.jpg": "1", "b.jpg": "2"})
    result = apply_plan(make_plan(folder, _cfg(rules_file)))
    (folder / "Images/a.jpg").write_text("edited since", encoding="utf-8")
    undo = make_undo_plan(result.journal)
    assert [s.src for s in undo.steps if s.kind == "move"] == ["Images/b.jpg"]
    assert "changed" in undo.skips[0].reason
    apply_undo(undo)
    assert snapshot(folder) == {"Images/a.jpg": "edited since", "b.jpg": "2"}
    assert not core.undone_marker(result.journal).exists()  # partial undo: can be retried


def test_undo_never_replaces_a_file_at_the_original_place(folder, rules_file):
    make_tree(folder, {"a.jpg": "1"})
    result = apply_plan(make_plan(folder, _cfg(rules_file)))
    make_tree(folder, {"a.jpg": "new file with the old name"})
    undo = make_undo_plan(result.journal)
    assert not [s for s in undo.steps if s.kind == "move"] and "taken" in undo.skips[0].reason
    apply_undo(undo)
    assert snapshot(folder) == {"a.jpg": "new file with the old name", "Images/a.jpg": "1"}


@pytest.mark.parametrize("bad", ["../escape.jpg", "/etc/passwd", "a/../../x", ".folder-tidy/run.jsonl", "a\\b", "",
                                 "C:/x/y.txt", "C:/x", "D:evil", "\\\\server\\share", "//server/share"])
def test_tampered_journal_paths_are_refused(folder, rules_file, bad):
    make_tree(folder, {"a.jpg": "1"})
    result = apply_plan(make_plan(folder, _cfg(rules_file)))
    lines = result.journal.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    rec["src"] = bad
    result.journal.write_text("\n".join(lines[:-1] + [json.dumps(rec)]) + "\n", encoding="utf-8")
    undo = make_undo_plan(result.journal)
    assert not [s for s in undo.steps if s.kind == "move"] and "unsafe" in undo.skips[0].reason


def test_undo_requires_a_journal_inside_a_journal_folder(tmp_path):
    j = tmp_path / "run.jsonl"
    j.write_text('{"format": 1}\n', encoding="utf-8")
    with pytest.raises(core.TidyError, match="not inside"):
        make_undo_plan(j)


def test_damaged_journal_is_reported(folder):
    j = folder / ".folder-tidy" / "run.jsonl"
    j.parent.mkdir()
    j.write_text('{"format": 1}\n' + "[" * 100000 + "\n", encoding="utf-8")
    with pytest.raises(core.TidyError, match="damaged"):
        make_undo_plan(j)
    j.write_text('{"format": 99}\n', encoding="utf-8")
    with pytest.raises(core.TidyError, match="incompatible"):
        make_undo_plan(j)


def test_interrupted_run_can_still_be_undone(folder, rules_file, monkeypatch):
    make_tree(folder, {"a.jpg": "1", "b.jpg": "2"})
    plan = make_plan(folder, _cfg(rules_file))
    real = core.move_no_clobber
    calls = []

    def fail_second(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError(5, "disk error")
        real(src, dst)
    monkeypatch.setattr(core, "move_no_clobber", fail_second)
    result = apply_plan(plan)
    monkeypatch.setattr(core, "move_no_clobber", real)
    assert len(result.moved) == 1 and len(result.failed) == 1
    apply_undo(make_undo_plan(result.journal))  # the journaled-but-not-done move is skipped quietly
    assert snapshot(folder) == {"a.jpg": "1", "b.jpg": "2"}


def test_two_runs_in_one_second_get_separate_journals(folder, rules_file):
    make_tree(folder, {"a.jpg": "1"})
    j1 = apply_plan(make_plan(folder, _cfg(rules_file))).journal
    make_tree(folder, {"b.pdf": "2"})
    j2 = apply_plan(make_plan(folder, _cfg(rules_file))).journal
    assert j1 != j2 and j1.exists() and j2.exists()


def test_journal_folder_that_is_a_link_is_refused(folder, rules_file, tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("this system doesn't allow creating symbolic links")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, folder / ".folder-tidy", target_is_directory=True)
    make_tree(folder, {"a.jpg": "1"})
    with pytest.raises(core.TidyError, match="symbolic link"):
        apply_plan(make_plan(folder, _cfg(rules_file)))
    assert (folder / "a.jpg").exists() and list(outside.iterdir()) == []


def test_failed_moves_are_marked_in_the_journal(folder, rules_file, monkeypatch):
    make_tree(folder, {"a.jpg": "1", "b.jpg": "2"})
    plan = make_plan(folder, _cfg(rules_file))
    real = core.move_no_clobber

    def fail_b(src, dst):
        if src.name == "b.jpg":
            raise OSError(5, "disk error")
        real(src, dst)
    monkeypatch.setattr(core, "move_no_clobber", fail_b)
    result = apply_plan(plan)
    recs = [json.loads(x) for x in result.journal.read_text(encoding="utf-8").splitlines()]
    assert {"op": "failed", "src": "b.jpg", "dst": "Images/b.jpg"} in recs


def test_unicode_spellings_of_one_name_collide(folder, rules_file):
    nfc, nfd = "caf\u00e9.jpg", "cafe\u0301.jpg"
    make_tree(folder, {f"x/{nfc}": "1", f"y/{nfd}": "2"})
    plan = make_plan(folder, _cfg(rules_file, recursive=True))
    assert len(plan.moves) == 1 and "another file is already going to" in plan.skips[0].reason


def test_unicode_spellings_match_the_same_rule():
    from folder_tidy.config import Rule
    assert Rule("C", frozenset(), ("caf\u00e9*",)).matches("cafe\u0301 menu.png")
    assert Rule("C", frozenset({"caf\u00e9"}), ()).matches("x.CAFE\u0301")


def test_file_changed_after_preview_is_left_alone(folder, rules_file):
    make_tree(folder, {"a.jpg": "1"})
    plan = make_plan(folder, _cfg(rules_file))
    (folder / "a.jpg").write_text("rewritten after the preview", encoding="utf-8")
    result = apply_plan(plan)
    assert not result.moved and "changed after the preview" in result.failed[0].reason
    assert snapshot(folder) == {"a.jpg": "rewritten after the preview"}


def test_file_swapped_between_link_and_unlink_is_not_deleted(tmp_path, monkeypatch):
    make_tree(tmp_path, {"a": "original"})
    real_link = os.link

    def link_then_swap(src, dst, *a, **k):
        real_link(src, dst, *a, **k)
        os.unlink(src)
        Path(src).write_text("someone else's new file", encoding="utf-8")
    monkeypatch.setattr(core.os, "link", link_then_swap)
    with pytest.raises(OSError, match="replaced while it was being moved"):
        move_no_clobber(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "a").read_text(encoding="utf-8") == "someone else's new file"
    assert not (tmp_path / "b").exists()


def test_folder_that_cannot_be_created_fails_cleanly(folder, rules_file, monkeypatch):
    make_tree(folder, {"a.jpg": "1", "b.pdf": "2"})
    before = snapshot(folder)
    plan = make_plan(folder, _cfg(rules_file))
    real_mkdir = Path.mkdir

    def no_images(self, *a, **k):
        if self.name == "Images":
            raise PermissionError(13, "Permission denied")
        return real_mkdir(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", no_images)
    result = apply_plan(plan)
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    assert [m.src for m in result.moved] == ["b.pdf"] and "couldn't create Images" in result.failed[0].reason
    res = apply_undo(make_undo_plan(result.journal))
    assert not res.failed and snapshot(folder) == before and not (folder / "Documents").exists()


@pytest.mark.parametrize("marker", ["package.json", "pyproject.toml", ".git"])
def test_software_project_folder_needs_force(folder, rules_file, marker):
    (folder / marker).mkdir() if marker == ".git" else (folder / marker).write_text("{}", encoding="utf-8")
    make_tree(folder, {"a.jpg": "1"})
    with pytest.raises(core.TidyError, match="software project"):
        make_plan(folder, _cfg(rules_file))
    assert [m.src for m in make_plan(folder, _cfg(rules_file), force=True).moves] == ["a.jpg"]


def test_home_folder_and_disk_top_need_force(folder, rules_file, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: folder))
    with pytest.raises(core.TidyError, match="home folder"):
        make_plan(folder, _cfg(rules_file))
    assert core.risky_folder_reason(Path(Path.cwd().anchor)) is not None


def test_recursive_never_enters_packages_or_projects(folder, rules_file):
    make_tree(folder, {"Photos.photoslibrary/a.jpg": "1", "Tool.app/Contents/b.png": "2",
                       "node_modules/c.png": "3", "proj/pyproject.toml": "x", "proj/d.jpg": "4",
                       "Report.pages/e.jpg": "5", "ok/f.jpg": "6"})
    plan = make_plan(folder, _cfg(rules_file, recursive=True))
    assert [(m.src, m.dst) for m in plan.moves] == [("ok/f.jpg", "Images/f.jpg")]
    assert {s.path for s in plan.skips} >= {"Photos.photoslibrary", "Tool.app", "node_modules", "proj",
                                             "Report.pages"}


@pytest.mark.skipif(os.name == "nt", reason="Windows can't create file names containing ':'")
def test_names_with_colon_are_left_alone(folder, rules_file):
    make_tree(folder, {"a:b.jpg": "1"})
    plan = make_plan(folder, _cfg(rules_file))
    assert not plan.moves and "can't be undone safely" in plan.skips[0].reason
