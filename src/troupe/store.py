"""Event-sourced configuration history, persisted under .troupe/."""

from __future__ import annotations

import os
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.application import AggregateNotFoundError, Application
from eventsourcing.domain import Aggregate, event

from .lineage import UNKNOWN, lineage, utc_timestamp
from .yamldoc import values_equal

CONFIG_ID = uuid5(NAMESPACE_URL, "troupe:configuration")

class Configuration(Aggregate):
    """Projection of the theatre configuration: its leaves and their history."""

    @staticmethod
    def create_id(*_: Any, **__: Any) -> UUID:
        return CONFIG_ID

    @event("Created")
    def __init__(self) -> None:
        self.leaves: dict[str, Any] = {}
        self.changes: list[dict[str, Any]] = []

    def _apply(self, op: str, path: str, old: Any, new: Any, meta: dict[str, Any] | None) -> None:
        seq = len(self.changes) + 1
        # events recorded before lineage existed carry none: say so rather than guess
        # (`at` is filled in from the stored event's timestamp by Troupe.configuration)
        meta = meta or {"actor": UNKNOWN, "via": "import" if op == "import" else None, "at": None,
                        "reason": None, "change": "unrecorded-import" if op == "import" else f"unrecorded-{seq}"}
        self.changes.append(
            {"seq": seq, "op": op, "path": path, "old": old, "new": new,
             "actor": dict(meta["actor"]), "via": meta["via"], "at": meta["at"],
             "reason": meta["reason"], "change": meta["change"]}
        )
        if op == "remove":
            self.leaves.pop(path, None)
        else:
            self.leaves[path] = new

    @event("Imported")
    def imported(self, path: str, new: Any, meta: dict[str, Any] | None = None) -> None:
        self._apply("import", path, None, new, meta)

    @event("ValueSet")
    def value_set(self, path: str, old: Any, new: Any, meta: dict[str, Any] | None = None) -> None:
        self._apply("set", path, old, new, meta)

    @event("ValueRemoved")
    def value_removed(self, path: str, old: Any, meta: dict[str, Any] | None = None) -> None:
        self._apply("remove", path, old, None, meta)


class Troupe(Application[UUID]):
    def __init__(self, state_dir: str):
        super().__init__(
            env={
                "PERSISTENCE_MODULE": "eventsourcing.sqlite",
                "SQLITE_DBNAME": os.path.join(state_dir, "events.sqlite"),
            }
        )

    def configuration(self) -> Configuration | None:
        try:
            config = self.repository.get(CONFIG_ID)
        except AggregateNotFoundError:
            return None
        if any(c["at"] is None for c in config.changes):
            stored = list(self.repository.event_store.get(CONFIG_ID))[1:]  # one per change, after Created
            for change, ev in zip(config.changes, stored):
                if change["at"] is None:
                    change["at"] = utc_timestamp(ev.timestamp)
        return config

    def ensure_imported(self, leaves: dict[str, Any]) -> Configuration:
        config = self.configuration()
        if config is None:
            config = Configuration()
            meta = lineage("import")
            for path, value in leaves.items():
                config.imported(path, value, meta)
            self.save(config)
        return config

    def reconcile(self, leaves: dict[str, Any]) -> Configuration:
        """Bring the projection in line with the files, recording what differs."""
        config = self.ensure_imported(leaves)
        self.record(config, dict(config.leaves), leaves, lineage("file"))
        return config

    def record(self, config: Configuration, old: dict[str, Any], new: dict[str, Any], meta: dict[str, Any]) -> None:
        for path, value in new.items():
            if path not in old:
                config.value_set(path, None, value, meta)
            elif not values_equal(old[path], value):
                config.value_set(path, old[path], value, meta)
        for path, value in old.items():
            if path not in new:
                config.value_removed(path, value, meta)
        if config.pending_events:
            self.save(config)
