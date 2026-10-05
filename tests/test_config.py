import json
import os
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
def test_get(theatre, path, out):
    for action in ("get", "show"):
        r = troupe(theatre, action, path)
        assert (r.returncode, r.stdout) == (0, out)


@pytest.mark.parametrize("path,code", [("agents", 5), ("tags", 5), ("nope", 5), ("name.x", 5)])
def test_get_errors(theatre, path, code):
    r = troupe(theatre, "get", path)
    assert r.returncode == code and r.stdout == b"" and path.encode() in r.stderr
    assert (b"config show" in r.stderr) == (path in ("agents", "tags"))
    if path.startswith("n"):
        assert troupe(theatre, "show", path).returncode == code


def test_show_sections(theatre):
    from ruamel.yaml import YAML
    r = troupe(theatre, "show", "agents")
    assert r.returncode == 0
    assert r.stdout.decode() == (
        "coder:\n  model: sonnet\n  tools:\n    - read\n    - write\n"
        "reviewer:\n  model: opus\n  prompt: |\n    Review.\n    Be nice.\n"
    )
    whole = YAML(typ="safe").load(troupe(theatre, "show").stdout)
    assert whole["flow"] == {"x": 1, "y": [1, 2]} and whole["quoted"] == "single"
    assert whole["agents"]["coder"]["tools"] == ["read", "write"]


def test_show_sources(theatre):
    r = troupe(theatre, "show", "--sources")
    assert r.returncode == 0
    lines = r.stdout.decode().splitlines()
    assert "name: demo # from .troupe/troupe.yml" in lines
    assert "    model: sonnet # from .troupe/agents/coder.yaml" in lines
    assert "      - write # from .troupe/agents/coder.yaml" in lines
    assert "    prompt: | # from .troupe/agents/prompt.md" in lines
    assert "agents:" in lines and "      Be nice." in lines
    assert troupe(theatre, "show", "tags", "--sources").stdout == (
        b"- a # from .troupe/troupe.yml\n- b # from .troupe/troupe.yml\n"
    )


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


def test_theatre_env_default(theatre, tmp_path_factory):
    run = lambda env, *extra: subprocess.run(
        [sys.executable, "-m", "troupe.cli_main", "config", "show", "name", *extra],
        capture_output=True, cwd=tmp_path_factory.mktemp("cwd"), env={**os.environ, **env},
    )
    assert run({"TROUPE_THEATRE": str(theatre)}).stdout == b"demo\n"
    assert run({"TROUPE_THEATRE": ""}).returncode == 4
    assert run({"TROUPE_THEATRE": "/nonexistent"}, "--theatre", str(theatre)).stdout == b"demo\n"


def events(theatre, path=None):
    r = troupe(theatre, "history", *([path] if path else []))
    assert r.returncode == 0, r.stderr
    return [(e["op"], e["path"], e["old"], e["new"]) for e in map(json.loads, r.stdout.decode().splitlines())]


def edits(theatre):
    return [e for e in events(theatre) if e[0] != "import"]


def test_reconcile_on_command(theatre):
    troupe(theatre, "show", "name")
    p = theatre / ".troupe/troupe.yml"
    p.write_text(cfg(theatre).replace("name: demo   # the name", "name:   demo # renamed comment\n# extra"))
    assert troupe(theatre, "show", "name").stdout == b"demo\n"
    assert edits(theatre) == []
    p.write_text(cfg(theatre).replace("demo", "play").replace("  - a\n", ""))
    assert edits(theatre) == []  # history does not reconcile
    assert troupe(theatre, "show", "name").stdout == b"play\n"
    assert edits(theatre) == [("set", "name", "demo", "play"), ("set", "tags.0", "a", "b"), ("remove", "tags.1", "b", None)]
    (theatre / ".troupe/agents/coder.yaml").write_text("model: opus\ntools: [read, write]\n")
    set_ok(theatre, "name", "x")
    assert edits(theatre)[3:] == [("set", "agents.coder.model", "sonnet", "opus"), ("set", "name", "play", "x")]


@pytest.fixture
def watcher(theatre):
    proc = subprocess.Popen(
        [sys.executable, "-m", "troupe.cli_main", "config", "watch", "--theatre", str(theatre)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.stdout.readline() == f"watching {theatre}\n".encode()
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def wait_for(fn, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while True:
        if fn():
            return
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.05)


def test_watch(theatre, watcher):
    import signal
    import time
    d = theatre / ".troupe"
    (d / "troupe.yml").write_text(cfg(theatre).replace("demo", "play"))
    wait_for(lambda: edits(theatre) == [("set", "name", "demo", "play")])
    (d / "agents/prompt.md").write_bytes(b"New.\r\n")
    wait_for(lambda: edits(theatre)[1:] == [("set", "agents.reviewer.prompt", "Review.\r\nBe nice.\r\n", "New.\r\n")])
    # a set while watching: only its own events
    set_ok(theatre, "agents.coder.model", "haiku")
    time.sleep(0.5)
    assert edits(theatre)[2:] == [("set", "agents.coder.model", "sonnet", "haiku")]
    # formatting-only edit: nothing
    (d / "troupe.yml").write_text(cfg(theatre).replace("quoted: 'single'", 'quoted: "single"  # q'))
    time.sleep(0.5)
    assert len(edits(theatre)) == 3
    # a hand-added include is followed, and then watched itself
    (d / "extra.yml").write_text("k: 1\n")
    (d / "troupe.yml").write_text(cfg(theatre) + "extra: file:extra.yml\n")
    wait_for(lambda: edits(theatre)[3:] == [("set", "extra.k", None, 1)])
    (d / "extra.yml").write_text("k: 2\n")
    wait_for(lambda: edits(theatre)[4:] == [("set", "extra.k", 1, 2)])
    # broken YAML is reported but the watch continues
    (d / "extra.yml").write_text("k: [\n")
    time.sleep(0.5)
    (d / "extra.yml").write_text("k: 3\n")
    wait_for(lambda: edits(theatre)[5:] == [("set", "extra.k", 2, 3)])
    watcher.send_signal(signal.SIGTERM)
    assert watcher.wait(timeout=5) == 0


def test_watch_sigint(theatre, watcher):
    import signal
    watcher.send_signal(signal.SIGINT)
    assert watcher.wait(timeout=5) == 0


def history(theatre, *path, env=None):
    r = subprocess.run(
        [sys.executable, "-m", "troupe.cli_main", "config", "history", *path, "--theatre", str(theatre)],
        capture_output=True, env={**os.environ, **(env or {})},
    )
    assert r.returncode == 0, r.stderr
    return [json.loads(line) for line in r.stdout.decode().splitlines()]


def set_as(theatre, actor, *args):
    env = {k: v for k, v in os.environ.items() if k != "TROUPE_ACTOR"}
    if actor is not None:
        env["TROUPE_ACTOR"] = actor
    return subprocess.run(
        [sys.executable, "-m", "troupe.cli_main", "config", "set", *args, "--theatre", str(theatre)],
        capture_output=True, env=env,
    )


def test_lineage(theatre):
    import getpass
    import re
    assert set_as(theatre, "agent:coder-7", "tags", "[x]", "--reason", "tidy tags").returncode == 0
    (theatre / ".troupe/troupe.yml").write_text(cfg(theatre).replace("demo", "play"))
    assert set_as(theatre, None, "name", "x").returncode == 0
    ev = history(theatre)
    imports = [e for e in ev if e["via"] == "import"]
    assert imports and {e["op"] for e in imports} == {"import"}
    assert len({e["change"] for e in imports}) == 1
    assert all(e["actor"] == {"kind": "unknown", "id": None} and e["reason"] is None for e in imports)
    agent = [e for e in ev if e["path"].startswith("tags.")][len([e for e in imports if e["path"].startswith("tags.")]):]
    assert [(e["op"], e["path"]) for e in agent] == [("set", "tags.0"), ("remove", "tags.1")]
    assert all(e["actor"] == {"kind": "agent", "id": "coder-7"} and e["via"] == "cli" and e["reason"] == "tidy tags" for e in agent)
    assert agent[0]["change"] == agent[1]["change"] != imports[0]["change"]
    filed, human = ev[-2], ev[-1]
    assert (filed["path"], filed["via"], filed["actor"], filed["reason"]) == ("name", "file", {"kind": "unknown", "id": None}, None)
    assert (human["path"], human["via"], human["actor"], human["reason"]) == ("name", "cli", {"kind": "human", "id": getpass.getuser()}, None)
    assert len({imports[0]["change"], agent[0]["change"], filed["change"], human["change"]}) == 4
    for e in ev:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", e["at"])
    assert [e["at"] for e in ev] == sorted(e["at"] for e in ev)


@pytest.mark.parametrize("actor", ["", "human", "human:", "robot:x", "agent"])
@pytest.mark.parametrize("args", [["set", "name", "x"], ["get", "name"], ["show"], ["history"], ["watch"]])
def test_bad_actor(theatre, actor, args):
    before = cfg(theatre)
    r = subprocess.run(
        [sys.executable, "-m", "troupe.cli_main", "config", *args, "--theatre", str(theatre)],
        capture_output=True, env={**os.environ, "TROUPE_ACTOR": actor}, timeout=10,
    )
    assert (r.returncode, r.stdout) == (2, b"") and b"TROUPE_ACTOR" in r.stderr
    assert cfg(theatre) == before
    assert not (theatre / ".troupe/events.sqlite").exists()  # nothing recorded, not even the import



def test_new_key_under_include_is_an_override(theatre):
    coder = theatre / ".troupe/agents/coder.yaml"
    coder.write_text("model: sonnet\nlimits:\n  tokens: 5\ntools: [read, write]\nsteps:\n  - name: a\n")
    shared = coder.read_text()
    p = theatre / ".troupe/troupe.yml"
    p.write_text(cfg(theatre).replace("coder: file:agents/coder.yaml", "coder: file:agents/coder.yaml  # c"))
    set_ok(theatre, "agents.coder.temperature", "0.3")
    assert "  coder:  # c\n    $ref: file:agents/coder.yaml\n    temperature: 0.3\n  reviewer:" in cfg(theatre)
    set_ok(theatre, "agents.coder.limits.time", "9")
    assert "    temperature: 0.3\n    limits:\n      time: 9\n  reviewer:" in cfg(theatre)
    assert coder.read_text() == shared
    # an overridden value changes the override; one that is not is changed where it lives
    set_ok(theatre, "agents.coder.temperature", "0.5")
    assert "    temperature: 0.5\n" in cfg(theatre)
    set_ok(theatre, "agents.coder.limits.tokens", "7")
    assert "  tokens: 7\n" in coder.read_text()
    # lists: appending, and new keys in list items, go to the file holding the list
    set_ok(theatre, "agents.coder.tools.2", "run")
    set_ok(theatre, "agents.coder.steps.0.n", "1")
    assert coder.read_text() == "model: sonnet\nlimits:\n  tokens: 7\ntools: [read, write, run]\nsteps:\n  - name: a\n    n: 1\n"
    assert troupe(theatre, "get", "agents.coder.limits.time").stdout == b"9\n"
    assert troupe(theatre, "get", "agents.coder.model").stdout == b"sonnet\n"


def test_mapping_form_includes(theatre):
    d = theatre / ".troupe"
    (d / "base.yaml").write_text("model: sonnet\nlimits: {tokens: 5, time: 1}\nlist: [1]\n")
    (d / "troupe.yml").write_text(
        "a:\n  $ref: file:base.yaml\n  model: opus\n  limits:\n    time: 2\n  list: [9]\n"
        "b: {$ref: file:base.yaml}\n"
        "c:\n  - file:base.yaml # ic\n"
        "t:\n  $ref: file:agents/prompt.md\n"
    )
    r = troupe(theatre, "show", "a")
    assert r.stdout == b"model: opus\nlimits:\n  tokens: 5\n  time: 2\nlist:\n  - 9\n"
    assert troupe(theatre, "get", "t").stdout == b"Review.\r\nBe nice.\r\n"
    set_ok(theatre, "a.model", "haiku")
    set_ok(theatre, "a.limits.x", "3")
    set_ok(theatre, "b.x", "4")
    set_ok(theatre, "c.0.x", "5")
    assert (d / "base.yaml").read_text() == "model: sonnet\nlimits: {tokens: 5, time: 1}\nlist: [1]\n"
    assert cfg(theatre) == (
        "a:\n  $ref: file:base.yaml\n  model: haiku\n  limits:\n    time: 2\n    x: 3\n  list: [9]\n"
        "b: {$ref: file:base.yaml, x: 4}\n"
        "c:\n  - $ref: file:base.yaml # ic\n    x: 5\n"
        "t:\n  $ref: file:agents/prompt.md\n"
    )
    (d / "troupe.yml").write_text(cfg(theatre) + "  x: 1\n")  # overrides on a text include
    r = troupe(theatre, "show")
    assert r.returncode == 4 and b"prompt.md" in r.stderr


def test_set_in_include(theatre):
    d = theatre / ".troupe"
    (d / "base.yaml").write_text("sub: {a: 1}\n")
    (d / "troupe.yml").write_text(cfg(theatre) + "b:\n  $ref: file:base.yaml\n  sub:\n    q: 1\n")
    for path, value in [("agents.coder.temperature", "0.3"), ("b.sub.z", "3"), ("b.w", "4"), ("b.sub.q", "5")]:
        r = troupe(theatre, "set", "--in-include", path, value)
        assert r.returncode == 0, r.stderr
    assert (d / "agents/coder.yaml").read_text() == "model: sonnet\ntools: [read, write]\ntemperature: 0.3\n"
    assert (d / "base.yaml").read_text() == "sub: {a: 1, z: 3}\nw: 4\n"
    assert "  coder: file:agents/coder.yaml\n" in cfg(theatre)
    assert cfg(theatre).endswith("b:\n  $ref: file:base.yaml\n  sub:\n    q: 5\n")


def test_set_in_include_refuses_to_disturb_other_includers(theatre):
    d = theatre / ".troupe"
    (d / "troupe.yml").write_text(cfg(theatre) + "twin: file:agents/coder.yaml\n")
    before = (cfg(theatre), (d / "agents/coder.yaml").read_text())
    r = troupe(theatre, "set", "--in-include", "agents.coder.temperature", "0.3")
    assert r.returncode == 3 and b"twin.temperature" in r.stderr
    assert (cfg(theatre), (d / "agents/coder.yaml").read_text()) == before
    set_ok(theatre, "agents.coder.temperature", "0.3")  # as an override it is fine
    assert troupe(theatre, "get", "twin.temperature").returncode == 5
