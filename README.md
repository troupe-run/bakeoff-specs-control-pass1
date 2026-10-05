# troupe

AI orchestration tool. Step 1: the configuration subsystem.

```
uv run troupe config get PATH [--theatre DIR]
uv run troupe config show [PATH] [--sources] [--theatre DIR]
uv run troupe config set PATH VALUE [--reason TEXT] [--in-include] [--theatre DIR]
uv run troupe config history [PATH] [--theatre DIR]
uv run troupe config watch [--theatre DIR]
```

Configuration lives in `<theatre>/.troupe/troupe.yml` (or `.yaml`); `file:` strings include other files.
`get` prints a single value; `show` prints a value or a whole section as YAML (includes resolved), and
with `--sources` tags each scalar with `# from <file>` (relative to the theatre). `set` writes changes back byte-surgically into whichever file holds the value.

An include may also be written in mapping form, `{$ref: file:x.yaml, …}`: the other keys are overrides, deep-merged
into the included content (mappings merge; scalars and lists replace). Included files are shared, so a new key under
included content is written as an override next to the nearest include reference (a scalar-form include is first
turned into mapping form); appended list items and new keys inside list items still go to the file holding the list.
`set --in-include` puts a new key into the included file itself instead (refused, exit 3, if that would change
settings elsewhere that include the same file). Choices made where the contract is silent are in `DECISIONS.md`.
Overrides on a non-YAML include are a load error (exit 4). Every change is recorded
as events (via `eventsourcing`, SQLite at `.troupe/events.sqlite`).

Files and the recorded configuration are kept in sync: every command except `history` first *reconciles*
(records a `set`/`remove` event per leaf that differs from the files), and `watch` polls the config file and
its includes and reconciles on change. Commands serialise on `.troupe/events.lock`, so a `set`'s own
write-back is already recorded by the time `watch` sees it and yields no further events.

Every recorded event carries its lineage: `actor` (`{"kind": "human"|"agent"|"unknown", "id": …}`), `via`
(`import`, `cli` or `file`), `at` (UTC, RFC 3339), `reason` (from `set --reason`) and `change` (shared by all events
of one command, file reconcile or import). A CLI command's actor comes from `TROUPE_ACTOR` (`human:<id>` or
`agent:<id>`; anything else makes every command exit 2), defaulting to the login name as a human. Imports and file edits have an
unknown actor. Events recorded before lineage existed report an unknown actor, their stored timestamp, and — for
`set`/`remove` — `via: null`, since whether they came from the CLI or a file was never recorded.

Layout: `yamldoc.py` (position-aware parse + include view), `render.py` (scalar/collection rendering, `show` output),
`editor.py` (write-back), `store.py` (event-sourced projection), `lineage.py` (actor, timestamp, change id),
`sync.py` (lock, reconcile, watch), `cli.py`.

Tests: `uv run pytest`.
