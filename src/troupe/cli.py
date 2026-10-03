"""`troupe config …` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from ruamel.yaml.nodes import SequenceNode

from .editor import Editor
from .errors import LoadError, PathError, TroupeError, UsageError, WriteRefused
from .yamldoc import VNode, compose, construct, leaves, values_equal


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # exit code 2 per contract
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    theatre = argparse.ArgumentParser(add_help=False)
    theatre.add_argument("--theatre", default=argparse.SUPPRESS, help="theatre directory (default: $TROUPE_THEATRE or .)")
    p = _Parser(prog="troupe")
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    config = sub.add_parser("config", parents=[theatre], help="inspect and change configuration")
    csub = config.add_subparsers(dest="action", required=True, parser_class=_Parser)
    show = csub.add_parser("show", parents=[theatre], help="print a setting")
    show.add_argument("path")
    st = csub.add_parser("set", parents=[theatre], help="change a setting")
    st.add_argument("path")
    st.add_argument("value")
    hist = csub.add_parser("history", parents=[theatre], help="list changes")
    hist.add_argument("path", nargs="?")
    csub.add_parser("watch", parents=[theatre], help="follow edits to the configuration files")
    return p


def parse_path(path: str) -> list[str]:
    segs = path.split(".")
    if not path or any(s == "" for s in segs):
        raise UsageError(f"invalid path: {path!r}")
    return segs


def parse_value(text: str) -> Any:
    if text.strip() == "":
        return None
    try:
        node = compose(f"[{text}]")
    except LoadError:
        raise UsageError(f"invalid value: {text!r}") from None
    if not isinstance(node, SequenceNode) or len(node.value) != 1:
        raise UsageError(f"invalid value: {text!r}")
    return construct(node.value[0])


def config_file(theatre: str) -> str:
    d = os.path.join(theatre, ".troupe")
    found = [p for p in (os.path.join(d, "troupe.yml"), os.path.join(d, "troupe.yaml")) if os.path.isfile(p)]
    if not found:
        raise LoadError(f"{os.path.join(d, 'troupe.yml')}: no configuration file")
    if len(found) > 1:
        raise LoadError(f"{found[0]}: both troupe.yml and troupe.yaml exist")
    return found[0]


def format_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value if value.endswith("\n") else value + "\n"
    if value is None:
        return "null\n"
    if isinstance(value, bool):
        return ("true" if value else "false") + "\n"
    if isinstance(value, float):
        return repr(value) + "\n"
    return f"{value}\n"


def lookup(root: VNode, segs: list[str]) -> VNode:
    node = root
    for i, s in enumerate(segs):
        c = node.child(s)
        if c is None:
            raise PathError(f"{'.'.join(segs)}: path not found")
        node = c
    return node


def out(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))


def run(args: argparse.Namespace) -> int:
    from .store import Troupe
    from .sync import locked, reconcile, watch

    theatre = getattr(args, "theatre", None)
    if theatre is None:
        theatre = os.environ.get("TROUPE_THEATRE") or "."
    if args.action == "watch":
        return watch(theatre, config_file, out)
    path_segs = parse_path(args.path) if getattr(args, "path", None) is not None else None
    value = parse_value(args.value) if args.action == "set" else None
    cfg = config_file(theatre)
    state_dir = os.path.dirname(cfg)
    with locked(state_dir):
        editor = Editor(cfg)
        app = Troupe(state_dir)
        if args.action == "history":  # reports what has been recorded; never reconciles
            root = None
            config = app.configuration() or app.ensure_imported(leaves(editor.load()))
        else:
            root, config = reconcile(app, editor)
        return act(args, path_segs, value, editor, root, app, config)


def act(args: argparse.Namespace, path_segs: list[str] | None, value: Any, editor: Editor, root: VNode | None, app: Any, config: Any) -> int:

    if args.action == "show":
        node = lookup(root, path_segs)
        if node.kind != "scalar":
            raise PathError(f"{args.path}: is a {'mapping' if node.kind == 'map' else 'list'}, not a scalar")
        out(format_scalar(node.value))
        return 0

    if args.action == "history":
        for change in config.changes:
            p = change["path"]
            if args.path is None or p == args.path or p.startswith(args.path + "."):
                out(json.dumps(change, ensure_ascii=False) + "\n")
        return 0

    # set
    old = leaves(root)
    editor.set(path_segs, value)
    new_root = editor.load()
    try:
        target = lookup(new_root, path_segs)
        result = target.py()
        if target.include == "text" and isinstance(result, str):
            result = result.replace("\r\n", "\n") if "\r\n" not in value else result
            value = value.replace("\r\n", "\n") if "\r\n" not in result else value
    except PathError:
        result = object()
    if not values_equal(result, value):
        raise WriteRefused(f"{args.path}: value cannot be written back faithfully")
    new = leaves(new_root)
    prefix = args.path + "."
    for p in set(old) | set(new):
        if p == args.path or p.startswith(prefix):
            continue
        if p not in old or p not in new or not values_equal(old[p], new[p]):
            raise WriteRefused(f"{args.path}: write-back would disturb {p}")
    for fpath, text in editor.changed().items():
        with open(fpath, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    app.record(config, old, new)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code = run(args)
    except TroupeError as e:
        sys.stderr.write(f"troupe: {e.message}\n")
        code = e.code
    sys.stdout.flush()
    return code
