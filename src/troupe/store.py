"""Event-sourced configuration history, persisted under .troupe/."""

from __future__ import annotations

import os
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.application import AggregateNotFoundError, Application
from eventsourcing.domain import Aggregate, event

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

    def _apply(self, op: str, path: str, old: Any, new: Any) -> None:
        self.changes.append(
            {"seq": len(self.changes) + 1, "op": op, "path": path, "old": old, "new": new}
        )
        if op == "remove":
            self.leaves.pop(path, None)
        else:
            self.leaves[path] = new

    @event("Imported")
    def imported(self, path: str, new: Any) -> None:
        self._apply("import", path, None, new)

    @event("ValueSet")
    def value_set(self, path: str, old: Any, new: Any) -> None:
        self._apply("set", path, old, new)

    @event("ValueRemoved")
    def value_removed(self, path: str, old: Any) -> None:
        self._apply("remove", path, old, None)


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
            return self.repository.get(CONFIG_ID)
        except AggregateNotFoundError:
            return None

    def ensure_imported(self, leaves: dict[str, Any]) -> Configuration:
        config = self.configuration()
        if config is None:
            config = Configuration()
            for path, value in leaves.items():
                config.imported(path, value)
            self.save(config)
        return config

    def record(self, config: Configuration, old: dict[str, Any], new: dict[str, Any]) -> None:
        for path, value in new.items():
            if path not in old:
                config.value_set(path, None, value)
            elif not values_equal(old[path], value):
                config.value_set(path, old[path], value)
        for path, value in old.items():
            if path not in new:
                config.value_removed(path, value)
        self.save(config)
