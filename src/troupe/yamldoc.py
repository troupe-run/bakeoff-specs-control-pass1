"""Position-aware YAML model.

ruamel.yaml is used only as a parser: it gives us the node tree with source
offsets.  Values are resolved with the YAML 1.2 core schema, and includes are
followed to build a merged *view* of the configuration whose nodes remember
which file (and which bytes) they come from.
"""

from __future__ import annotations

import bisect
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .errors import LoadError

_yaml = YAML(typ="safe", pure=True)

# --------------------------------------------------------------------------
# Core schema

_NULL = re.compile(r"null|Null|NULL|~|")
_TRUE = re.compile(r"true|True|TRUE")
_FALSE = re.compile(r"false|False|FALSE")
_INT10 = re.compile(r"[-+]?[0-9]+")
_INT8 = re.compile(r"0o[0-7]+")
_INT16 = re.compile(r"0x[0-9a-fA-F]+")
_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
_INF = re.compile(r"[-+]?\.(inf|Inf|INF)")
_NAN = re.compile(r"\.(nan|NaN|NAN)")


def resolve_plain(s: str) -> Any:
    if _NULL.fullmatch(s):
        return None
    if _TRUE.fullmatch(s):
        return True
    if _FALSE.fullmatch(s):
        return False
    if _INT10.fullmatch(s):
        return int(s)
    if _INT8.fullmatch(s):
        return int(s[2:], 8)
    if _INT16.fullmatch(s):
        return int(s[2:], 16)
    if _FLOAT.fullmatch(s):
        return float(s)
    if _INF.fullmatch(s):
        return -math.inf if s.startswith("-") else math.inf
    if _NAN.fullmatch(s):
        return math.nan
    return s


def construct(node: Node | None) -> Any:
    if node is None:
        return None
    if isinstance(node, ScalarNode):
        return resolve_plain(node.value) if node.style is None else node.value
    if isinstance(node, MappingNode):
        return {key_text(k): construct(v) for k, v in node.value}
    if isinstance(node, SequenceNode):
        return [construct(v) for v in node.value]
    raise TypeError(node)


def key_text(node: Node) -> str:
    if isinstance(node, ScalarNode):
        return node.value
    raise LoadError("complex mapping keys are not supported")


def compose(text: str, name: str = "<value>") -> Node | None:
    try:
        return _yaml.compose(text)
    except YAMLError as e:
        raise LoadError(f"{name}: invalid YAML: {e}") from None


def values_equal(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(values_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


# --------------------------------------------------------------------------
# Documents


class Doc:
    """One YAML file's text plus line bookkeeping."""

    def __init__(self, path: str, text: str):
        self.path = path
        self.text = text
        m = re.search(r"\r?\n", text)
        self.newline = m.group(0) if m else "\n"
        self.line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
        self.root = compose(text, path)

    # line helpers -------------------------------------------------------
    def line_of(self, pos: int) -> int:
        return bisect.bisect_right(self.line_starts, pos) - 1

    def line_start(self, pos: int) -> int:
        return self.line_starts[self.line_of(pos)]

    def col(self, pos: int) -> int:
        return pos - self.line_start(pos)

    def eol(self, pos: int) -> int:
        """Offset of the line break ending the line containing pos."""
        i = self.text.find("\n", pos)
        if i < 0:
            return len(self.text)
        if i > 0 and self.text[i - 1] == "\r":
            return i - 1
        return i

    def next_line(self, pos: int) -> int:
        """Offset of the start of the line after the one containing pos."""
        i = self.text.find("\n", pos)
        return len(self.text) if i < 0 else i + 1

    def line_text(self, n: int) -> str:
        s = self.line_starts[n]
        return self.text[s : self.eol(s)]

    @property
    def nlines(self) -> int:
        return len(self.line_starts)


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_block_scalar(node: Node | None) -> bool:
    return isinstance(node, ScalarNode) and node.style in ("|", ">")


def is_empty_null(node: Node | None) -> bool:
    return (
        isinstance(node, ScalarNode)
        and node.style is None
        and node.value == ""
        and node.start_mark.index == node.end_mark.index
    )


def is_flow(node: Node | None) -> bool:
    return isinstance(node, (MappingNode, SequenceNode)) and bool(node.flow_style)


# --------------------------------------------------------------------------
# View


@dataclass(eq=False)
class VNode:
    kind: str  # "map", "seq", "scalar"
    doc: Doc  # document holding this node's content
    node: Node | None  # ruamel node in `doc` (None: empty document)
    src_doc: Doc  # document holding the node's position in its parent
    src_node: Node | None  # ruamel node in `src_doc`
    parent: "VNode | None" = None
    key_node: Node | None = None  # for mapping entries
    anchor: int | None = None  # offset just after ':' or '-' in src_doc
    anchor_indent: int = 0  # column of the key / dash in src_doc
    flow_parent: bool = False
    include: str | None = None  # None, "yaml" or "text"
    include_path: str | None = None
    value: Any = None  # scalar value
    children: dict[str, "VNode"] | list["VNode"] = field(default_factory=dict)
    end: int = 0  # end of content in src_doc

    def py(self) -> Any:
        if self.kind == "map":
            return {k: v.py() for k, v in self.children.items()}
        if self.kind == "seq":
            return [v.py() for v in self.children]
        return self.value

    def child(self, seg: str) -> "VNode | None":
        if self.kind == "map":
            return self.children.get(seg)
        if self.kind == "seq" and seg.isdigit():
            i = int(seg)
            return self.children[i] if i < len(self.children) else None
        return None

    def entries(self) -> list["VNode"]:
        if self.kind == "map":
            return list(self.children.values())
        if self.kind == "seq":
            return list(self.children)
        return []

    @property
    def start(self) -> int:
        """Start of this entry in src_doc (key or dash)."""
        if self.key_node is not None:
            return self.key_node.start_mark.index
        if self.anchor is not None and not self.flow_parent:
            return self.anchor - 1
        return value_start(self)


def value_start(v: VNode) -> int:
    if v.src_node is None or is_empty_null(v.src_node):
        return v.anchor if v.anchor is not None else 0
    return v.src_node.start_mark.index


def content_end(doc: Doc, node: Node | None, anchor: int | None, children_end: int | None) -> int:
    if node is None:
        return len(doc.text.rstrip())
    if is_empty_null(node):
        return anchor if anchor is not None else node.start_mark.index
    if is_block_scalar(node):
        p = node.end_mark.index
        s = node.start_mark.index
        while p > s and doc.text[p - 1] in " \t\r\n":
            p -= 1
        return p
    if isinstance(node, ScalarNode) or is_flow(node):
        return node.end_mark.index
    assert children_end is not None
    return children_end


def find_colon(doc: Doc, pos: int) -> int:
    """Offset just after the ':' following a key ending at pos."""
    t = doc.text
    i = pos
    while i < len(t) and t[i] in " \t":
        i += 1
    if i < len(t) and t[i] == ":":
        return i + 1
    return pos


def dash_positions(doc: Doc, node: SequenceNode) -> list[int]:
    start = node.start_mark.index
    end = node.end_mark.index
    col = doc.col(start)
    out = [start]
    for n in range(doc.line_of(start) + 1, doc.line_of(end) + 1):
        line = doc.line_text(n)
        if indent_of(line) == col and line[col : col + 1] == "-" and line[col + 1 : col + 2] in ("", " ", "\t"):
            out.append(doc.line_starts[n] + col)
    return out


Reader = Callable[[str], str]


class Loader:
    def __init__(self, read: Reader):
        self.read = read
        self.docs: dict[str, Doc] = {}

    def doc(self, path: str) -> Doc:
        if path not in self.docs:
            self.docs[path] = Doc(path, self.read(path))
        return self.docs[path]

    def load(self, path: str) -> VNode:
        doc = self.doc(path)
        if doc.root is None:
            v = VNode("map", doc, None, doc, None)
            v.end = content_end(doc, None, None, None)
            return v
        return self.build(doc, doc.root, doc, doc.root, None, None, None, 0, False, (os.path.realpath(path),))

    def build(
        self,
        doc: Doc,
        node: Node,
        src_doc: Doc,
        src_node: Node,
        parent: VNode | None,
        key_node: Node | None,
        anchor: int | None,
        anchor_indent: int,
        flow_parent: bool,
        stack: tuple[str, ...],
    ) -> VNode:
        v = VNode("scalar", doc, node, src_doc, src_node, parent, key_node, anchor, anchor_indent, flow_parent)
        if isinstance(node, ScalarNode):
            value = construct(node)
            if isinstance(value, str) and value.startswith("file:"):
                return self.include(v, value[5:], stack)
            v.value = value
        elif isinstance(node, MappingNode):
            v.kind = "map"
            children: dict[str, VNode] = {}
            last_end = None
            for k, cn in node.value:
                a = find_colon(doc, k.end_mark.index)
                c = self.build(doc, cn, doc, cn, v, k, a, doc.col(k.start_mark.index), bool(node.flow_style), stack)
                children[key_text(k)] = c
                last_end = c.end
            v.children = children
            v.end = content_end(doc, node, anchor, last_end)
            if doc is not src_doc:
                v.end = content_end(src_doc, src_node, anchor, None)
            return v
        elif isinstance(node, SequenceNode):
            v.kind = "seq"
            items: list[VNode] = []
            last_end = None
            if node.flow_style:
                for cn in node.value:
                    c = self.build(doc, cn, doc, cn, v, None, None, 0, True, stack)
                    items.append(c)
                    last_end = c.end
            else:
                dashes = dash_positions(doc, node)
                if len(dashes) != len(node.value):
                    raise LoadError(f"{doc.path}: unsupported sequence layout")
                for d, cn in zip(dashes, node.value):
                    c = self.build(doc, cn, doc, cn, v, None, d + 1, doc.col(d), False, stack)
                    items.append(c)
                    last_end = c.end
            v.children = items
            v.end = content_end(doc, node, anchor, last_end)
            if doc is not src_doc:
                v.end = content_end(src_doc, src_node, anchor, None)
            return v
        v.end = content_end(src_doc, src_node, anchor, None)
        return v

    def include(self, v: VNode, rel: str, stack: tuple[str, ...]) -> VNode:
        base = os.path.dirname(v.doc.path)
        target = os.path.normpath(os.path.join(base, rel))
        real = os.path.realpath(target)
        if real in stack:
            raise LoadError(f"{target}: include cycle (from {v.doc.path})")
        try:
            if target.endswith((".yml", ".yaml")):
                tdoc = self.doc(target)
            else:
                text = self.read(target)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise LoadError(f"{target}: included file not found (from {v.doc.path})") from None
        except UnicodeDecodeError:
            raise LoadError(f"{target}: included file is not UTF-8 (from {v.doc.path})") from None
        if not target.endswith((".yml", ".yaml")):
            v.include = "text"
            v.include_path = target
            v.value = text
            v.end = content_end(v.src_doc, v.src_node, v.anchor, None)
            return v
        if tdoc.root is None:
            v.include = "yaml"
            v.include_path = target
            v.value = None
            v.end = content_end(v.src_doc, v.src_node, v.anchor, None)
            return v
        inner = self.build(
            tdoc, tdoc.root, v.src_doc, v.src_node, v.parent, v.key_node, v.anchor, v.anchor_indent,
            v.flow_parent, stack + (real,),
        )
        inner.include = "yaml"
        inner.include_path = target
        if inner.kind == "scalar":
            inner.end = content_end(v.src_doc, v.src_node, v.anchor, None)
        return inner


def leaves(v: VNode, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}

    def walk(n: VNode, p: str) -> None:
        if n.kind == "map":
            for k, c in n.children.items():
                walk(c, f"{p}.{k}" if p else k)
        elif n.kind == "seq":
            for i, c in enumerate(n.children):
                walk(c, f"{p}.{i}" if p else str(i))
        else:
            out[p] = n.value

    walk(v, prefix)
    return out
