import json
import subprocess
import sys
from pathlib import Path

import pytest

CONFIG = """\
# Troupe config
name: demo   # the name
quoted: 'single'
agents:
  coder: file:agents/coder.yaml
  reviewer:
    model: "opus"
    prompt: file:agents/prompt.md
tags:
  - a
  - "b"
  # trailing comment
desc: >
  This is a folded
  paragraph of text.

  Second para.
lit: |
  line one
  line two
flow: {x: 1, y: [1, 2]}  # c
"""


def troupe(theatre, *args):
    return subprocess.run(
        [sys.executable, "-m", "troupe.cli_main", "config", *args, "--theatre", str(theatre)],
        capture_output=True,
    )


@pytest.fixture
def theatre(tmp_path):
    d = tmp_path / ".troupe"
    (d / "agents").mkdir(parents=True)
    (d / "troupe.yml").write_text(CONFIG)
    (d / "agents" / "coder.yaml").write_text("model: sonnet\ntools: [read, write]\n")
    (d / "agents" / "prompt.md").write_bytes(b"Review.\r\nBe nice.\r\n")
    return tmp_path


def cfg(theatre):
    return (theatre / ".troupe" / "troupe.yml").read_text()


def set_ok(theatre, path, value):
    r = troupe(theatre, "set", path, value)
    assert r.returncode == 0, r.stderr
    assert r.stdout == b""


@pytest.mark.parametrize(
    "path,out",
    [
        ("name", b"demo\n"),
        ("agents.coder.model", b"sonnet\n"),
        ("agents.reviewer.prompt", b"Review.\r\nBe nice.\r\n"),
        ("tags.1", b"b\n"),
        ("desc", b"This is a folded paragraph of text.\nSecond para.\n"),
        ("flow.y.1", b"2\n"),
    ],
)
def test_show(theatre, path, out):
    r = troupe(theatre, "show", path)
    assert (r.returncode, r.stdout) == (0, out)


@pytest.mark.parametrize("path,code", [("agents", 5), ("tags", 5), ("nope", 5), ("name.x", 5)])
def test_show_errors(theatre, path, code):
    r = troupe(theatre, "show", path)
    assert r.returncode == code and r.stdout == b"" and path.encode() in r.stderr


def test_set_keeps_comment_column(theatre):
    set_ok(theatre, "name", "x")
    assert "name: x      # the name\n" in cfg(theatre)
    set_ok(theatre, "name", "a-much-longer-value")
    assert "name: a-much-longer-value # the name\n" in cfg(theatre)


def test_set_quoting(theatre):
    set_ok(theatre, "quoted", "it's")
    assert "quoted: 'it''s'\n" in cfg(theatre)
    set_ok(theatre, "tags.2", "c")
    assert '  - "b"\n  # trailing comment\n  - "c"\n' in cfg(theatre)


def test_set_include_targets(theatre):
    set_ok(theatre, "agents.coder.model", "haiku")
    assert (theatre / ".troupe/agents/coder.yaml").read_text() == "model: haiku\ntools: [read, write]\n"
    set_ok(theatre, "agents.reviewer.prompt", '"Hi\\nthere\\n"')
    assert (theatre / ".troupe/agents/prompt.md").read_bytes() == b"Hi\r\nthere\r\n"
    assert troupe(theatre, "set", "agents.reviewer.prompt", "5").returncode == 3
    before = cfg(theatre)
    assert troupe(theatre, "set", "agents.coder", "x").returncode == 3
    assert cfg(theatre) == before


def test_block_scalars(theatre):
    set_ok(theatre, "lit", '"line one\\nline 2"')
    assert "lit: |-\n  line one\n  line 2\nflow" in cfg(theatre)
    set_ok(theatre, "desc", '"This is a folded paragraph of text.\\nSecond para now longer.\\n"')
    assert "  This is a folded\n  paragraph of text.\n\n  Second para\n  now longer.\n" in cfg(theatre)


def test_new_keys_and_collections(theatre):
    set_ok(theatre, "agents.reviewer.limits.tokens", "100")
    assert '    prompt: file:agents/prompt.md\n    limits:\n      tokens: 100\ntags' in cfg(theatre)
    set_ok(theatre, "flow.z", "3")
    assert "flow: {x: 1, y: [1, 2], z: 3} # c\n" in cfg(theatre)
    assert troupe(theatre, "set", "tags.5", "x").returncode == 5


def test_history(theatre):
    set_ok(theatre, "tags", "[a]")
    r = troupe(theatre, "history", "tags")
    events = [json.loads(line) for line in r.stdout.decode().splitlines()]
    assert [(e["op"], e["path"], e["old"], e["new"]) for e in events] == [
        ("import", "tags.0", None, "a"),
        ("import", "tags.1", None, "b"),
        ("remove", "tags.1", "b", None),
    ]
    set_ok(theatre, "tags", "[a]")  # no change, no events
    assert len(troupe(theatre, "history", "tags").stdout.splitlines()) == 3


def test_missing_config(tmp_path):
    assert troupe(tmp_path, "show", "x").returncode == 4
