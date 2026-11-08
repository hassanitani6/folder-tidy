import json
import re
from pathlib import Path

from conftest import make_tree, snapshot
from folder_tidy import doctor
from folder_tidy.cli import main


def test_init_writes_example_and_refuses_to_overwrite(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    text = (tmp_path / "folder-tidy.toml").read_text(encoding="utf-8")
    assert "[[rules]]" in text
    (tmp_path / "folder-tidy.toml").write_text("mine", encoding="utf-8")
    assert main(["init"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert (tmp_path / "folder-tidy.toml").read_text(encoding="utf-8") == "mine"
    assert main(["init", "--overwrite"]) == 0
    assert (tmp_path / "folder-tidy.toml").read_text(encoding="utf-8") == text
    assert not list(tmp_path.glob("*.part"))


def test_sort_previews_by_default(folder, rules_file, capsys):
    make_tree(folder, {"a.jpg": "1"})
    before = snapshot(folder)
    assert main(["sort", str(folder), "-c", str(rules_file())]) == 0
    out = capsys.readouterr().out
    assert "MOVE   a.jpg -> Images/a.jpg" in out and "Nothing was changed" in out
    assert snapshot(folder) == before


def test_sort_apply_and_undo_round_trip(folder, rules_file, capsys):
    make_tree(folder, {"a.jpg": "1", "b.pdf": "2"})
    before = snapshot(folder)
    assert main(["sort", str(folder), "-c", str(rules_file()), "--apply"]) == 0
    out = capsys.readouterr().out
    journal = re.search(r'folder-tidy undo "(.+)"', out).group(1)
    assert snapshot(folder) == {"Images/a.jpg": "1", "Documents/b.pdf": "2"}
    assert main(["undo", journal]) == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert snapshot(folder) == {"Images/a.jpg": "1", "Documents/b.pdf": "2"}
    assert main(["undo", journal, "--apply"]) == 0
    assert snapshot(folder) == before
    assert main(["undo", journal, "--apply"]) == 2
    assert "already undone" in capsys.readouterr().err


def test_bad_rules_give_a_clean_error(folder, rules_file, capsys):
    rules = rules_file('[[rules]]\nto = "../outside"\nextensions = ["jpg"]\n')
    assert main(["sort", str(folder), "-c", str(rules)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err


def test_missing_folder_and_rules(tmp_path, rules_file, capsys):
    assert main(["sort", str(tmp_path / "nope"), "-c", str(rules_file())]) == 2
    assert "not a folder" in capsys.readouterr().err
    assert main(["sort", str(tmp_path), "-c", str(tmp_path / "none.toml")]) == 2
    assert "not found" in capsys.readouterr().err


def test_doctor_runs_and_redacts(capsys):
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tool_version"] and "current_folder" in report
    assert doctor.redact("/Users/someone/x and a@b.example token=abc") == "/Users/<user>/x and <email> token=<redacted>"
    assert str(Path.home()) not in doctor.redact(str(Path.home() / "file"))


def test_sort_refuses_risky_folder_without_force(folder, rules_file, capsys):
    make_tree(folder, {"package.json": "{}", "a.jpg": "1"})
    rules = rules_file()
    assert main(["sort", str(folder), "-c", str(rules)]) == 2
    assert "--force" in capsys.readouterr().err
    assert main(["sort", str(folder), "-c", str(rules), "--force"]) == 0
