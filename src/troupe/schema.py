"""The configuration's JSON Schema (`.troupe/troupe.schema.json`)."""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .errors import LoadError

SCHEMA_NAME = "troupe.schema.json"


class Schema:
    def __init__(self, path: str, text: str):
        self.path = path
        try:
            self.root = json.loads(text)
        except json.JSONDecodeError as e:
            raise LoadError(f"{path}: invalid JSON: {e}") from None
        try:
            Draft202012Validator.check_schema(self.root)
        except SchemaError as e:
            raise LoadError(f"{path}: invalid JSON Schema: {e.message}") from None
        self.validator = Draft202012Validator(self.root)

    def validate(self, value: Any) -> None:
        """Raise LoadError if `value` does not match the schema."""
        errors = sorted(self.validator.iter_errors(value), key=lambda e: list(map(str, e.absolute_path)))
        if errors:
            e = errors[0]
            where = ".".join(map(str, e.absolute_path)) or "configuration"
            raise LoadError(f"{where}: {e.message} (schema {self.path})")

    def is_uri(self, path: tuple[str, ...] | list[str]) -> bool:
        """Whether the schema for the value at `path` has `"format": "uri"`."""
        return any(s.get("format") == "uri" for s in self.at(path))

    def at(self, path: tuple[str, ...] | list[str]) -> list[dict]:
        """Subschemas describing the value at `path`, through properties, items and additionalProperties."""
        current = self.expand(self.root)
        for seg in path:
            nxt: list[Any] = []
            for s in current:
                props = s.get("properties")
                if isinstance(props, dict) and seg in props:
                    nxt.append(props[seg])
                else:
                    pattern = s.get("patternProperties")
                    matched = [v for p, v in pattern.items() if re.search(p, seg)] if isinstance(pattern, dict) else []
                    nxt += matched
                    if not matched and isinstance(s.get("additionalProperties"), dict):
                        nxt.append(s["additionalProperties"])
                if seg.isdigit():
                    i = int(seg)
                    prefix = s.get("prefixItems")
                    if isinstance(prefix, list) and i < len(prefix):
                        nxt.append(prefix[i])
                    elif isinstance(s.get("items"), dict):
                        nxt.append(s["items"])
            current = [x for n in nxt for x in self.expand(n)]
            if not current:
                break
        return current

    def expand(self, s: Any, depth: int = 0) -> list[dict]:
        """A schema with its local `$ref` and `allOf`/`anyOf`/`oneOf` branches."""
        if not isinstance(s, dict) or depth > 32:
            return []
        out = [s]
        ref = s.get("$ref")
        if isinstance(ref, str) and ref.startswith("#"):
            target = self.pointer(ref[1:])
            out += self.expand(target, depth + 1)
        for k in ("allOf", "anyOf", "oneOf"):
            if isinstance(s.get(k), list):
                for b in s[k]:
                    out += self.expand(b, depth + 1)
        return out

    def pointer(self, ptr: str) -> Any:
        node: Any = self.root
        for part in ptr.split("/")[1:] if ptr else []:
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return None
        return node
