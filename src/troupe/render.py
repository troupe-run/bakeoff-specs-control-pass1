"""Rendering Python values as YAML text."""

from __future__ import annotations

import math
import unicodedata
from typing import Any

from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .yamldoc import _yaml, resolve_plain

PLAIN, SINGLE, DOUBLE = "plain", "'", '"'

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\0": "\\0",
    "\a": "\\a",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\v": "\\v",
    "\f": "\\f",
    "\r": "\\r",
    "\x1b": "\\e",
    "\x85": "\\N",
}


def is_control(ch: str) -> bool:
    return unicodedata.category(ch) == "Cc"


def style_of(node: Any) -> str | None:
    """Quoting style of an existing scalar node, or None if not a flow scalar."""
    if isinstance(node, ScalarNode):
        if node.style is None:
            return PLAIN
        if node.style in (SINGLE, DOUBLE):
            return node.style
    return None


def render_non_string(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ".nan"
        if math.isinf(value):
            return ".inf" if value > 0 else "-.inf"
        return repr(value)
    raise TypeError(value)


def _parses_to(text: str, value: str, style: str | None, flow: bool) -> bool:
    try:
        node = _yaml.compose(f"[{text}]" if flow else f"k: {text}")
    except YAMLError:
        return False
    if flow:
        if not isinstance(node, SequenceNode) or len(node.value) != 1:
            return False
        n = node.value[0]
    else:
        if not isinstance(node, MappingNode) or len(node.value) != 1:
            return False
        n = node.value[0][1]
    return isinstance(n, ScalarNode) and n.style == style and n.value == value


def plain(s: str, flow: bool) -> str | None:
    if not s or s != s.strip(" \t") or any(is_control(c) or c in "\u2028\u2029\ufeff" for c in s):
        return None
    if not isinstance(resolve_plain(s), str):
        return None
    if s[0] in ",[]{}#&*!|>'\"%@`":
        return None
    if s[0] in "-?:" and (len(s) == 1 or s[1] in " \t" or (flow and s[1] in ",[]{}")):
        return None
    if ": " in s or " #" in s or s.endswith(":"):
        return None
    if flow and any(c in s for c in ",[]{}"):
        return None
    return s if _parses_to(s, s, None, flow) else None


def single(s: str, flow: bool) -> str | None:
    if "\n" in s or any(is_control(c) and c != "\t" for c in s) or any(c in "\u2028\u2029\ufeff" for c in s):
        return None
    out = "'" + s.replace("'", "''") + "'"
    return out if _parses_to(out, s, SINGLE, flow) else None


def double(s: str) -> str:
    out = []
    for c in s:
        if c in _ESCAPES:
            out.append(_ESCAPES[c])
        elif is_control(c):
            out.append(f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def scalar(value: Any, style: str | None, flow: bool) -> str:
    """Render a scalar on one line, preferring `style`, else the minimal one."""
    if not isinstance(value, str):
        return render_non_string(value)
    order = [PLAIN, SINGLE, DOUBLE]
    if style in order:
        order.remove(style)
        order.insert(0, style)
    for st in order:
        if st == PLAIN:
            r = plain(value, flow)
        elif st == SINGLE:
            r = single(value, flow)
        else:
            r = double(value)
        if r is not None:
            return r
    raise AssertionError


def is_multiline(value: Any) -> bool:
    return isinstance(value, str) and "\n" in value


def literal_ok(value: str) -> bool:
    if any((is_control(c) and c not in "\t\n") or c in "\u2028\u2029\ufeff" for c in value):
        return False
    body = value.rstrip("\n")
    lines = body.split("\n")
    first = next((ln for ln in lines if ln), None)
    if first is None:
        return False
    if first[0] in " \t":
        return False
    # leading empty lines must stay empty: fine since we write them bare
    return True


def chomp_for(value: str, old: str = "") -> str:
    t = len(value) - len(value.rstrip("\n"))
    if t == 0:
        return "-"
    if t == 1:
        return old if old in ("", "+") else ""
    return "+"


def literal_lines(value: str, indent: int) -> list[str]:
    body = value.rstrip("\n")
    t = len(value) - len(body)
    lines = [(" " * indent + ln) if ln else "" for ln in body.split("\n")]
    lines += [""] * max(0, t - 1)
    return lines


class Renderer:
    """Renders new values using a document's layout conventions."""

    def __init__(self, step: int = 2, seq_offset: int | None = None):
        self.step = step
        self.seq_offset = step if seq_offset is None else seq_offset

    def flow(self, value: Any, style: str | None = None) -> str:
        if isinstance(value, dict):
            return "{" + ", ".join(f"{scalar(k, None, True)}: {self.flow(v)}" for k, v in value.items()) + "}"
        if isinstance(value, list):
            return "[" + ", ".join(self.flow(v) for v in value) + "]"
        if is_multiline(value):
            return double(value)
        return scalar(value, style, True)

    def value_after_indicator(self, value: Any, indent: int, style: str | None, compact: bool) -> tuple[str, list[str]]:
        """Render a value following `key:` or `-` whose line is indented `indent`.

        Returns the text for the indicator's line (without leading space) and
        the following lines.
        """
        if isinstance(value, (dict, list)) and value:
            if compact:
                lines = self.block(value, indent + 2)
                return lines[0][indent + 2 :], lines[1:]
            sub = indent + (self.step if isinstance(value, dict) else self.seq_offset)
            return "", self.block(value, sub)
        if isinstance(value, (dict, list)):
            return ("{}" if isinstance(value, dict) else "[]"), []
        if is_multiline(value) and literal_ok(value):
            return "|" + chomp_for(value), literal_lines(value, indent + self.step)
        if is_multiline(value):
            return double(value), []
        return scalar(value, style, False), []

    def entry(self, key: str, value: Any, indent: int, kstyle: str | None = None, vstyle: str | None = None) -> list[str]:
        head, rest = self.value_after_indicator(value, indent, vstyle, False)
        k = scalar(key, kstyle, False)
        return [" " * indent + k + ":" + (" " + head if head else "")] + rest

    def item(self, value: Any, indent: int, vstyle: str | None = None) -> list[str]:
        head, rest = self.value_after_indicator(value, indent, vstyle, True)
        return [" " * indent + "-" + (" " + head if head else "")] + rest

    def block(self, value: Any, indent: int) -> list[str]:
        lines: list[str] = []
        if isinstance(value, dict):
            for k, v in value.items():
                lines += self.entry(k, v, indent)
        else:
            for v in value:
                lines += self.item(v, indent)
        return lines
