"""Position-aware YAML model.

ruamel.yaml is used only as a parser: it gives us the node tree with source
offsets.  Values are resolved with the YAML 1.2 core schema, and includes are
followed to build a merged *view* of the configuration whose nodes remember
which file (and which bytes) they come from.
"""

from __future__ import annotations

import bisect
import copy
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

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


LITERAL_TAG = "!literal"


class Literal(str):
    """A string written tagged `!literal`: taken as it is, never as an include."""


def is_literal_tagged(node: Node | None) -> bool:
    return isinstance(node, ScalarNode) and node.tag == LITERAL_TAG


def construct(node: Node | None, tags: bool = False) -> Any:
    """The value of a node; with `tags`, `!literal` strings come back as `Literal`."""
    if node is None:
        return None
    if isinstance(node, ScalarNode):
        if is_literal_tagged(node):
            return Literal(node.value) if tags else node.value
        return resolve_plain(node.value) if node.style is None else node.value
    if isinstance(node, MappingNode):
        return {key_text(k): construct(v, tags) for k, v in node.value}
    if isinstance(node, SequenceNode):
        return [construct(v, tags) for v in node.value]
    raise TypeError(node)


def is_remote(path: str) -> bool:
    return path.startswith(("https://", "http://"))


def include_spec(value: Any) -> str | None:
    """What a string read as an include points at (`file:` path or URL), else None."""
    if isinstance(value, str):
        if value.startswith("file:"):
            return value[5:]
        if is_remote(value):
            return value
    return None


def is_yaml_target(target: str) -> bool:
    path = urlparse(target).path if is_remote(target) else target
    return path.endswith((".yml", ".yaml"))


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
    if (str if isinstance(a, str) else type(a)) is not (str if isinstance(b, str) else type(b)):
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
        self.props_end: dict[int, int] = {}  # id(node) -> offset just after its `&anchor`
        self._skip_anchors(self.root, set())

    def _skip_anchors(self, node: Node | None, seen: set[int]) -> None:
        """Start anchored nodes at their content, so that writing a value keeps the `&anchor`."""
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        if getattr(node, "anchor", None):
            s = node.start_mark.index
            m = re.compile(r"&[^\s,\[\]{}]+").match(self.text, s)
            if m:
                self.props_end[id(node)] = i = m.end()
                mark = copy.copy(node.start_mark)
                if isinstance(node, ScalarNode) and node.style is None and node.value == "":
                    mark.index = i  # `key: &a` alone: an empty value right after the anchor
                    end = copy.copy(node.end_mark)
                    end.index = i
                    node.end_mark = end
                else:
                    mark.index = skip_space(self.text, i, True)
                node.start_mark = mark
        if isinstance(node, MappingNode):
            for k, v in node.value:
                self._skip_anchors(v, seen)
        elif isinstance(node, SequenceNode):
            for v in node.value:
                self._skip_anchors(v, seen)

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


def skip_space(text: str, i: int, lines: bool) -> int:
    """Skip blanks (and, if `lines`, line breaks and comments) from i."""
    while i < len(text):
        c = text[i]
        if c in " \t":
            i += 1
        elif lines and c in "\r\n":
            i += 1
        elif lines and c == "#" and (i == 0 or text[i - 1] in " \t\r\n"):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
        else:
            break
    return i


_ALIAS = re.compile(r"\*([^\s,\[\]{}]+)")


def alias_at(text: str, pos: int) -> tuple[int, int, str] | None:
    """The `*name` alias token following pos (after ':', '-', ',' or '['): start, end, name."""
    i = pos
    while True:
        i = skip_space(text, i, True)
        if i < len(text) and text[i] in ",[{":
            i += 1
            continue
        break
    m = _ALIAS.match(text, i)
    return (m.start(), m.end(), m.group(1)) if m else None


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
    container: Node | None = None  # ruamel collection in src_doc holding this entry
    include_doc: Doc | None = None  # for include roots: the included YAML document
    ref_entry: "VNode | None" = None  # for mapping-form includes: the `$ref` entry itself
    base: "VNode | None" = None  # for merged mappings: the included mapping the overrides merge into
    dash: int | None = None  # for block list items: offset of the '-'
    alias: str | None = None  # name of the anchor, if this value is written as an alias `*name`
    alias_span: tuple[int, int] | None = None  # the `*name` token in src_doc
    path: tuple[str, ...] = ()  # path of this value in the configuration
    tagged: bool = False  # written tagged `!literal`

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
        if self.dash is not None:
            return self.dash
        return value_start(self)


def value_start(v: VNode) -> int:
    if v.alias_span is not None:
        return v.alias_span[0]
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
    def __init__(self, read: Reader, literal: Callable[[tuple[str, ...]], bool] | None = None):
        self.read = read
        self.literal = literal or (lambda path: False)  # the schema says the string at path is a URI
        self.docs: dict[str, Doc] = {}
        # remote document -> (local file, include string as written there) through which it is included
        self.remote_refs: dict[str, tuple[str, str]] = {}

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
        src_end: int | None = None,
        path: tuple[str, ...] = (),
    ) -> VNode:
        def end_in_src() -> int:
            return src_end if src_end is not None else content_end(src_doc, src_node, anchor, None)

        v = VNode("scalar", doc, node, src_doc, src_node, parent, key_node, anchor, anchor_indent, flow_parent)
        v.path = path
        if isinstance(node, ScalarNode):
            value = construct(node)
            v.tagged = is_literal_tagged(node)
            if not v.tagged and include_spec(value) is not None and not self.literal(path):
                return self.include(v, value, stack, end_in_src())
            v.value = value
        elif isinstance(node, MappingNode) and self.ref_of(node, path) is not None:
            return self.ref_include(v, node, stack, src_end)
        elif isinstance(node, MappingNode):
            v.kind = "map"
            children: dict[str, VNode] = {}
            last_end = None
            for k, cn in node.value:
                a = find_colon(doc, k.end_mark.index)
                alias = cn.start_mark.index < k.end_mark.index  # an alias points back at its anchor
                if not alias:
                    a = doc.props_end.get(id(cn), a)
                c = self.build(
                    doc, cn, doc, cn, v, k, a, doc.col(k.start_mark.index), bool(node.flow_style), stack,
                    path=path + (key_text(k),),
                )
                if alias:
                    mark_alias(c, doc, a)
                c.container = node
                children[key_text(k)] = c
                last_end = c.end
            v.children = children
            v.end = content_end(doc, node, anchor, last_end)
            if doc is not src_doc:
                v.end = end_in_src()
            return v
        elif isinstance(node, SequenceNode):
            v.kind = "seq"
            items: list[VNode] = []
            last_end = None
            if node.flow_style:
                prev = node.start_mark.index + 1
                for cn in node.value:
                    alias = cn.start_mark.index < prev
                    c = self.build(doc, cn, doc, cn, v, None, None, 0, True, stack, path=path + (str(len(items)),))
                    if alias:
                        mark_alias(c, doc, prev)
                    c.container = node
                    items.append(c)
                    last_end = prev = c.end
            else:
                dashes = dash_positions(doc, node)
                if len(dashes) != len(node.value):
                    raise LoadError(f"{doc.path}: unsupported sequence layout")
                for d, cn in zip(dashes, node.value):
                    alias = cn.start_mark.index < d
                    a = d + 1 if alias else doc.props_end.get(id(cn), d + 1)
                    c = self.build(doc, cn, doc, cn, v, None, a, doc.col(d), False, stack, path=path + (str(len(items)),))
                    if alias:
                        mark_alias(c, doc, a)
                    c.dash = d
                    c.container = node
                    items.append(c)
                    last_end = c.end
            v.children = items
            v.end = content_end(doc, node, anchor, last_end)
            if doc is not src_doc:
                v.end = end_in_src()
            return v
        v.end = end_in_src()
        return v

    def include(self, v: VNode, ref: str, stack: tuple[str, ...], src_end: int, overrides: bool = False) -> VNode:
        """Follow the include string `ref` (`file:…` or a URL) written in v's document."""
        spec = include_spec(ref)
        if is_remote(spec):
            target = spec
        elif is_remote(v.doc.path):  # a relative include inside a remote document is remote too
            target = urljoin(v.doc.path, spec)
        else:
            target = os.path.normpath(os.path.join(os.path.dirname(v.doc.path), spec))
        real = target if is_remote(target) else os.path.realpath(target)
        if real in stack:
            raise LoadError(f"{target}: include cycle (from {v.doc.path})")
        if is_remote(target):
            via = self.remote_refs.get(v.doc.path) if is_remote(v.doc.path) else (v.doc.path, ref)
            if via is not None:
                self.remote_refs.setdefault(target, via)
        yaml = is_yaml_target(target)
        try:
            if yaml:
                tdoc = self.doc(target)
            else:
                text = self.read(target)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise LoadError(f"{target}: included file not found (from {v.doc.path})") from None
        except UnicodeDecodeError:
            raise LoadError(f"{target}: included file is not UTF-8 (from {v.doc.path})") from None
        if not yaml:
            if overrides:
                raise LoadError(f"{v.doc.path}: overrides on {target}, which is not a YAML include")
            v.include = "text"
            v.include_path = target
            v.value = text
            v.end = src_end
            return v
        if tdoc.root is None:
            v.include = "yaml"
            v.include_path = target
            v.include_doc = tdoc
            v.value = None
            v.end = src_end
            return v
        inner = self.build(
            tdoc, tdoc.root, v.src_doc, v.src_node, v.parent, v.key_node, v.anchor, v.anchor_indent,
            v.flow_parent, stack + (real,), src_end, v.path,
        )
        if inner.include is None:  # else the included file is itself an include: the nearer one counts
            inner.include = "yaml"
            inner.include_path = target
            inner.include_doc = tdoc
        return inner

    def ref_of(self, node: MappingNode, path: tuple[str, ...]) -> tuple[Node, Node] | None:
        """The `$ref: <include>` entry making a mapping an include, if any."""
        for k, cn in node.value:
            if isinstance(k, ScalarNode) and k.value == "$ref" and isinstance(cn, ScalarNode):
                if is_literal_tagged(cn) or self.literal(path + ("$ref",)):
                    continue
                if include_spec(construct(cn)) is not None:
                    return k, cn
        return None

    def ref_include(self, v: VNode, node: MappingNode, stack: tuple[str, ...], src_end: int | None) -> VNode:
        """A mapping-form include: `$ref: file:…` plus overrides deep-merged into the included content."""
        doc = v.doc
        ref_k, ref_v = self.ref_of(node, v.path)
        a = find_colon(doc, ref_k.end_mark.index)
        over = VNode("map", doc, node, v.src_doc, v.src_node, v.parent, v.key_node, v.anchor, v.anchor_indent, v.flow_parent)
        over.path = v.path
        ref = VNode("scalar", doc, ref_v, doc, ref_v, over, ref_k, a, doc.col(ref_k.start_mark.index), bool(node.flow_style))
        ref.value = construct(ref_v)
        ref.end = content_end(doc, ref_v, a, None)
        ref.container = node
        children: dict[str, VNode] = {}
        last_end = ref.end
        for k, cn in node.value:
            if k is ref_k:
                continue
            ka = find_colon(doc, k.end_mark.index)
            c = self.build(
                doc, cn, doc, cn, over, k, ka, doc.col(k.start_mark.index), bool(node.flow_style), stack,
                path=v.path + (key_text(k),),
            )
            c.container = node
            children[key_text(k)] = c
            last_end = max(last_end, c.end)
        if src_end is None:
            src_end = content_end(doc, node, v.anchor, last_end) if doc is v.src_doc else content_end(v.src_doc, v.src_node, v.anchor, None)
        over.end = src_end
        inc = self.include(v, ref.value, stack, src_end, overrides=bool(children))
        if inc.include == "text" or (not children and inc.kind != "map"):
            return inc
        over.include, over.include_path, over.include_doc = "yaml", inc.include_path, inc.include_doc
        over.ref_entry = ref
        over.base = inc if inc.kind == "map" else None
        over.children = merge(inc.children if inc.kind == "map" else {}, children, over)
        return over


def mark_alias(v: VNode, doc: Doc, pos: int) -> None:
    """Record that v is written at pos as an alias: its own text is just the `*name` token."""
    found = alias_at(doc.text, pos)
    if found is None:
        raise LoadError(f"{doc.path}: unsupported alias layout")
    s, e, v.alias = found
    v.alias_span = (s, e)
    v.end = e


def alias_twins(root: VNode, segs: list[str]) -> set[str]:
    """Paths that are the same written value as `segs`, because they reach it through aliases of one anchor.

    Empty if the path itself goes through an alias.
    """
    node, i = root, 0
    while i < len(segs):
        c = node.child(segs[i])
        if c is None:
            break
        node, i = c, i + 1
        if node.alias is not None:
            return set()
    rest = segs[i:]
    sigs: dict[int, tuple] = {}

    def walk(n: VNode, p: list[str], chain: tuple) -> None:
        if n.include is not None:
            chain = chain + ((n.src_doc.path, id(n.src_node)),)
        sig = (n.doc.path, id(n.node), n.kind, chain)
        sigs.setdefault(id(n), sig)
        if sig == target_sig[0]:
            out.add(".".join(p + rest))
        if n.kind == "map":
            for k, c in n.children.items():
                walk(c, p + [k], chain)
        elif n.kind == "seq":
            for j, c in enumerate(n.children):
                walk(c, p + [str(j)], chain)

    # first pass finds the target's signature, second collects its twins
    out: set[str] = set()
    target_sig: list = [None]
    walk(root, [], ())
    target_sig[0] = sigs[id(node)]
    out.clear()
    walk(root, [], ())
    return out


def merge(base: dict[str, VNode], over: dict[str, VNode], parent: VNode) -> dict[str, VNode]:
    """Deep-merge override entries into included ones: mappings merge, anything else replaces."""
    out = dict(base)
    for k, o in over.items():
        b = out.get(k)
        if b is not None and b.kind == "map" and o.kind == "map":
            o.children = merge(b.children, o.children, o)
            o.base = b
        out[k] = o
    for c in out.values():
        c.parent = parent
    return out


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
