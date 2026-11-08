from pathlib import Path

import pytest

from folder_tidy.config import ConfigError, Rule, destination_errors, load, parse


def test_example_rules_file_is_valid():
    import tomllib
    from importlib import resources
    cfg = parse(tomllib.loads(resources.files("folder_tidy").joinpath("example.toml").read_text("utf-8")))
    assert cfg.rules[0].to == "Images/Screenshots"
    assert cfg.on_conflict == "skip" and cfg.recursive is False


@pytest.mark.parametrize("to", ["/etc", "C:/Windows", "C:", "../outside", "a/../../b", "./x", ".hidden",
                                "a\\b", "CON", "nul.txt", "Docs/COM1", "trailing.", "trailing ", "a:b",
                                "", "   ", 5, None, "a//b", "x/", "//server/share", "\\\\server\\share", "D:evil"])
def test_unsafe_destinations_rejected(to):
    assert destination_errors(to)


def test_parent_folder_reference_is_named_in_the_error():
    assert "'..'" in destination_errors("../outside")[0]


@pytest.mark.parametrize("to", ["Images", "Documents/PDFs", "2026 Photos", "Café", "con artist"])
def test_ordinary_destinations_accepted(to):
    assert destination_errors(to) == []


def test_every_problem_is_reported_at_once():
    with pytest.raises(ConfigError) as e:
        parse({"recursive": "yes", "on_conflict": "replace", "colour": 1,
               "rules": [{"to": "../x", "extensions": ["jpg"]}, {"to": "Ok"}, {"to": "A", "extensions": []},
                         {"to": "B", "names": ["*"], "size": 1}]})
    msg = str(e.value)
    for expected in ("'recursive' must be true or false", "on_conflict", "unknown setting 'colour'",
                     "rule 1", "rule 2: give 'extensions'", "rule 3: 'extensions' is empty", "rule 4: unknown key"):
        assert expected in msg


def test_no_rules_is_an_error():
    with pytest.raises(ConfigError, match="at least one"):
        parse({})


def test_extensions_are_normalised():
    cfg = parse({"rules": [{"to": "I", "extensions": [".JPG", " png "]}]})
    assert cfg.rules[0].extensions == {"jpg", "png"}


def test_rule_matching():
    r = Rule("I", frozenset({"jpg"}), ())
    assert r.matches("a.JPG") and not r.matches("a.jpeg") and not r.matches("jpg")
    assert not Rule("I", frozenset({"bashrc"}), ()).matches(".bashrc")  # a dot-file has no extension
    n = Rule("S", frozenset(), ("screenshot*",))
    assert n.matches("Screenshot 2026.png") and not n.matches("my screenshot.png")
    both = Rule("S", frozenset({"png"}), ("screen*",))
    assert both.matches("screen.png") and not both.matches("screen.jpg")


def test_bad_toml_and_missing_file(tmp_path: Path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[[rules]\nto=", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(bad)
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "missing.toml")


def test_oversized_rules_file_rejected(tmp_path: Path):
    big = tmp_path / "big.toml"
    big.write_text("# x\n" * 300_000, encoding="utf-8")
    with pytest.raises(ConfigError, match="larger than 1 MB"):
        load(big)
