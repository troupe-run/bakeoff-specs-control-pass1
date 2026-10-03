"""Keeping the recorded configuration in sync with the files under .troupe/."""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from .editor import Editor
from .errors import LoadError, TroupeError
from .store import Configuration, Troupe
from .yamldoc import VNode, leaves

CONFIG_NAMES = ("troupe.yml", "troupe.yaml")
POLL = 0.1  # seconds between looks at the files
SETTLE = 0.05  # wait for a file being written to stop changing


@contextmanager
def locked(state_dir: str) -> Iterator[None]:
    """Serialise reconcile/write/record across troupe processes."""
    fd = os.open(os.path.join(state_dir, "events.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def reconcile(app: Troupe, editor: Editor) -> tuple[VNode, Configuration]:
    """Load the files and record a set/remove event for each leaf that differs."""
    root = editor.load()
    return root, app.reconcile(leaves(root))


def read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


Snapshot = dict[str, "bytes | None"]


def snapshot(paths: list[str]) -> Snapshot:
    return {p: read_bytes(p) for p in paths}


def watch(theatre: str, config_file: Callable[[str], str], out: Callable[[str], None]) -> int:
    state_dir = os.path.join(theatre, ".troupe")
    candidates = [os.path.join(state_dir, n) for n in CONFIG_NAMES]
    stop = False

    def on_signal(signum: int, frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    config_file(theatre)  # fail early (exit 4) when there is nothing to watch
    app: Troupe | None = None

    def sync(initial: bool = False) -> Snapshot:
        """Reconcile; return the content of every file the result depends on."""
        nonlocal app
        seen = snapshot(candidates)
        editor: Editor | None = None
        try:
            with locked(state_dir):
                seen = snapshot(candidates)
                editor = Editor(config_file(theatre))
                app = app or Troupe(state_dir)
                reconcile(app, editor)
        except (TroupeError, OSError, UnicodeDecodeError) as e:
            if initial:
                raise e if isinstance(e, TroupeError) else LoadError(str(e)) from None
            sys.stderr.write(f"troupe: {getattr(e, 'message', e)}\n")
            sys.stderr.flush()
        if editor is not None:
            seen.update(editor.seen)
        return seen

    current = sync(initial=True)
    out(f"watching {theatre}\n")
    sys.stdout.flush()
    while not stop:
        time.sleep(POLL)
        if stop:
            break
        now = snapshot(list(current))
        if now == current:
            continue
        for _ in range(20):
            time.sleep(SETTLE)
            again = snapshot(list(current))
            if again == now:
                break
            now = again
        current = sync()
    return 0
