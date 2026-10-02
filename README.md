# troupe

AI orchestration tool. Step 1: the configuration subsystem.

```
uv run troupe config show PATH [--theatre DIR]
uv run troupe config set PATH VALUE [--theatre DIR]
uv run troupe config history [PATH] [--theatre DIR]
```

Configuration lives in `<theatre>/.troupe/troupe.yml` (or `.yaml`); `file:` strings include other files.
`set` writes changes back byte-surgically into whichever file holds the value. Every change is recorded
as events (via `eventsourcing`, SQLite at `.troupe/events.sqlite`).

Layout: `yamldoc.py` (position-aware parse + include view), `render.py` (scalar/collection rendering),
`editor.py` (write-back), `store.py` (event-sourced projection), `cli.py`.

Tests: `uv run pytest`.
