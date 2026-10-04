"""Lineage of a change: who made it, how, when, why, and which command it belongs to."""

from __future__ import annotations

import getpass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .errors import UsageError

UNKNOWN = {"kind": "unknown", "id": None}


def login_name() -> str | None:
    try:
        return getpass.getuser() or None
    except Exception:
        return None


def utc_timestamp(when: datetime | None = None) -> str:
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def lineage(via: str, actor: dict[str, Any] | None = None, reason: str | None = None) -> dict[str, Any]:
    """Who/how/when/why for one command; every event it produces shares the `change` id."""
    return {
        "actor": dict(actor or UNKNOWN),
        "via": via,
        "at": utc_timestamp(),
        "reason": reason,
        "change": str(uuid4()),
    }


def parse_actor(env: str | None) -> dict[str, Any]:
    """TROUPE_ACTOR is `human:<id>` or `agent:<id>`; unset means the logged-in human."""
    if env is None:
        return {"kind": "human", "id": login_name()}
    kind, sep, ident = env.partition(":")
    if not sep or kind not in ("human", "agent") or not ident:
        raise UsageError(f"invalid TROUPE_ACTOR: {env!r} (expected human:<id> or agent:<id>)")
    return {"kind": kind, "id": ident}
