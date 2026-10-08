"""Byte-surgical write-back of configuration changes."""

from __future__ import annotations

import difflib
import os
import re
import urllib.error
import urllib.request
from typing import Any

from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .errors import LoadError, PathError, WriteRefused
from .render import (
    Renderer,
    chomp_for,
    double,
    is_multiline,
    literal_ok,
    scalar,
    style_of,
)
from .schema import SCHEMA_NAME, Schema
from .yamldoc import (
    Doc,
    construct,
    include_spec,
    is_literal_tagged,
    is_remote,
    Literal,
    Loader,
    VNode,
    indent_of,
    is_block_scalar,
    is_empty_null,
    is_flow,
    value_start,
    values_equal,
)

Edit = tuple[int, int, str, bool]


# --------------------------------------------------------------------------
# text helpers


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _eol(text: str, pos: int) -> int:
    i = text.find("\n", pos)
    if i < 0:
        return len(text)
    return i - 1 if i > 0 and text[i - 1] == "\r" else i


def find_comment(text: str, pos: int) -> int | None:
    """Offset of a comment starting at or after pos on pos's line."""
    end = _eol(text, pos)
    i = pos
    while i < end:
        c = text[i]
        prev = text[i - 1] if i > 0 else "\n"
        if c == "#" and prev in " \t\n":
            return i
        if c in "'\"" and prev in " \t[{,:\n":
            j = i + 1
            while j < end:
                if c == "'" and text[j] == "'":
                    if text[j + 1 : j + 2] == "'":
                        j += 2
                        continue
                    break
                if c == '"' and text[j] == "\\":
                    j += 2
                    continue
                if c == '"' and text[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return None


def splice(text: str, start: int, end: int, new: str, fix_comment: bool) -> str:
    """Replace text[start:end] by new, keeping an end-of-line comment's column."""
    if fix_comment and "\n" not in new:
        cpos = find_comment(text, end)
        if cpos is not None:
            ls = _line_start(text, start)
            old_col = cpos - _line_start(text, end)
            head = text[ls:start] + new + text[end:cpos].rstrip(" \t")
            pad = old_col - len(head) if len(head) + 1 <= old_col else 1
            return text[:ls] + head + " " * pad + text[cpos:]
    return text[:start] + new + text[end:]


def header_end(text: str, s: int) -> int:
    i = s + 1
    while i < len(text) and text[i] in "0123456789+-":
        i += 1
    return i


def tag_end(text: str, s: int) -> int:
    """Offset just after a `!tag ` written at s (s itself if there is none)."""
    m = re.compile(r"![^\s,\[\]{}]*[ \t]+").match(text, s)
    return m.end() if m else s


FETCH_TIMEOUT = 30  # seconds


def fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LoadError(f"{url}: cannot fetch remote include: {getattr(e, 'reason', e)}") from None


def wrap(text: str, width: int) -> list[str]:
    words = text.split(" ")
    lines = [words[0]]
    for w in words[1:]:
        if len(lines[-1]) + 1 + len(w) <= width:
            lines[-1] += " " + w
        else:
            lines.append(w)
    return lines


def layout(doc: Doc) -> Renderer:
    found: dict[str, int] = {}

    def walk(node: Node | None, parent_col: int | None) -> None:
        if isinstance(node, MappingNode) and not node.flow_style and node.value:
            c = doc.col(node.value[0][0].start_mark.index)
            if parent_col is not None and c > parent_col:
                found.setdefault("step", c - parent_col)
            for k, v in node.value:
                walk(v, doc.col(k.start_mark.index))
        elif isinstance(node, SequenceNode) and not node.flow_style and node.value:
            c = doc.col(node.start_mark.index)
            if parent_col is not None and c >= parent_col:
                found.setdefault("seq", c - parent_col)
            for v in node.value:
                walk(v, None)

    walk(doc.root, None)
    step = found.get("step", 2)
    return Renderer(step, found.get("seq", step))


def _split_lines(chunk: str) -> list[str]:
    if not chunk:
        return []
    lines = chunk.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _join(lines: list[str], nl: str, final: bool = True) -> str:
    if not lines:
        return ""
    return nl.join(lines) + (nl if final else "")


# --------------------------------------------------------------------------


class Editor:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.overlay: dict[str, str] = {}
        self.disk: dict[str, str] = {}
        self.seen: dict[str, bytes | None] = {}  # raw bytes of every file read (None: unreadable)
        self.remote_refs: dict[str, tuple[str, str]] = {}  # remote document -> (local file, include string)
        self.schema_path = os.path.join(os.path.dirname(config_path), SCHEMA_NAME)
        self.schema: Schema | None = None
        self.path = ""  # the path being set, for messages

    # files -------------------------------------------------------------
    def read(self, path: str) -> str:
        if path in self.overlay:
            return self.overlay[path]
        if is_remote(path):  # fetched once per command; never watched
            if path not in self.disk:
                self.disk[path] = fetch(path).decode("utf-8")
            return self.disk[path]
        if path not in self.disk:
            self.seen[path] = None
            with open(path, "rb") as f:
                raw = f.read()
            self.seen[path] = raw
            self.disk[path] = raw.decode("utf-8")
        return self.disk[path]

    def load_schema(self) -> Schema | None:
        if self.schema is None and os.path.isfile(self.schema_path):
            try:
                self.schema = Schema(self.schema_path, self.read(self.schema_path))
            except UnicodeDecodeError:
                raise LoadError(f"{self.schema_path}: not UTF-8") from None
        elif self.schema is None:
            self.seen.setdefault(self.schema_path, None)  # so that watch notices one being added
        return self.schema

    def load(self) -> VNode:
        schema = self.load_schema()
        loader = Loader(self.read, schema.is_uri if schema is not None else None)
        root = loader.load(self.config_path)
        for url, via in loader.remote_refs.items():
            self.remote_refs.setdefault(url, via)
        return root

    def validate(self, root: VNode) -> None:
        """Raise LoadError if the configuration does not match the schema."""
        schema = self.load_schema()
        if schema is not None:
            schema.validate(root.py())

    def is_uri(self, segs: list[str]) -> bool:
        schema = self.load_schema()
        return schema is not None and schema.is_uri(segs)

    def literals(self, segs: list[str], value: Any) -> Any:
        """`value` with every string that would be read as an include made a `!literal`, unless a URI there."""
        if isinstance(value, dict):
            return {k: self.literals(segs + [k], v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.literals(segs + [str(i)], v) for i, v in enumerate(value)]
        if include_spec(value) is not None and not isinstance(value, Literal) and not self.is_uri(segs):
            return Literal(value)
        return value

    def writable(self, path: str) -> None:
        """Refuse to change a remote document, naming the include line to change instead."""
        if is_remote(path):
            local, ref = self.remote_refs.get(path, (None, path))
            where = f" in {local}" if local else ""
            raise WriteRefused(
                f"{self.path}: value comes from remote include {ref}, which troupe cannot change; "
                f"change the include line {ref}{where} instead"
            )

    def changed(self) -> dict[str, str]:
        return {p: t for p, t in self.overlay.items() if self.disk.get(p) != t}

    def apply(self, doc: Doc, edits: list[Edit]) -> None:
        self.writable(doc.path)
        text = doc.text
        for s, e, new, fix in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
            text = splice(text, s, e, new, fix)
        self.overlay[doc.path] = text

    # operations ----------------------------------------------------------
    def set(self, segs: list[str], value: Any, in_include: bool = False, expand_alias: bool = False) -> None:
        """Set a value; `in_include` puts new keys in the included file rather than an override.

        A path reached through an alias is refused unless `expand_alias`, which first replaces the
        alias by a copy of the anchored value.
        """
        path = self.path = self.path or ".".join(segs)
        if "<<" in segs:
            raise WriteRefused(f"{path}: merge keys (<<) are never written")
        node = self.load()
        i = 0
        while i < len(segs):
            c = node.child(segs[i])
            if c is None:
                break
            node = c
            i += 1
            if node.alias is not None:
                at = ".".join(segs[:i])
                if not expand_alias:
                    raise WriteRefused(
                        f"{path}: {at} is an alias of &{node.alias}, so this would change every use of it; "
                        f"set the anchored value, or use --expand-alias to give {at} its own copy"
                    )
                if i == len(segs):  # the alias itself: its copy is the new value
                    self.expand(node, value, at)
                    return
                self.expand(node, construct(node.src_node, tags=True), at)
                self.set(segs, value, in_include, expand_alias)
                return
        if i == len(segs):
            self.assign(segs, node, value, in_include, expand_alias)
        else:
            self.create(segs, node, i, value, in_include)

    def expand(self, vn: VNode, value: Any, path: str) -> None:
        """Replace the alias `vn` by a block-style `value`, marking the key's line."""
        if vn.flow_parent:
            raise WriteRefused(f"{path}: cannot expand an alias inside a flow collection")
        doc = vn.src_doc
        text = doc.text
        nl = doc.newline
        r = layout(doc)
        s, e = vn.alias_span
        head, lines = r.value_after_indicator(value, vn.anchor_indent, style_of(vn.src_node), False)
        if isinstance(value, (dict, list)) and value and vn.key_node is None:  # list item: content below the dash
            lines = r.block(value, vn.anchor_indent + 2)
        rest = text[e : doc.eol(e)].strip()
        line = (" " + head if head else "") + f"  # expanded from *{vn.alias} by troupe" + ("  " + rest if rest else "")
        edits: list[Edit] = [(vn.anchor, doc.eol(e), line, False)]
        if lines:
            pos = doc.next_line(e)
            pre = nl if pos == len(text) and not text.endswith("\n") else ""
            edits.append((pos, pos, pre + _join(lines, nl), False))
        self.apply(doc, edits)

    def assign(self, segs: list[str], vn: VNode, value: Any, in_include: bool = False, expand_alias: bool = False) -> None:
        path = ".".join(segs)
        if vn.include == "text":
            if not isinstance(value, str):
                raise WriteRefused(f"{path}: text include {vn.include_path} can only be set to a string")
            if value == vn.value:
                return
            self.writable(vn.include_path)
            new = value
            if "\r\n" in vn.value:
                new = re.sub(r"(?<!\r)\n", "\r\n", value)
            self.overlay[vn.include_path] = new
            return
        if values_equal(vn.py(), value):
            return
        if vn.include == "yaml":
            self.writable(vn.include_path)
            raise WriteRefused(f"{path}: value comes from include {vn.include_path}")
        if vn.kind == "map" and isinstance(value, dict) and value and vn.children:
            old_keys = list(vn.children)
            for k, v in value.items():
                self.set(segs + [k], v, in_include, expand_alias)
            for k in old_keys:
                if k not in value:
                    self.remove(segs + [k])
            return
        if vn.kind == "seq" and isinstance(value, list) and value and vn.children:
            n = len(vn.children)
            for i, v in enumerate(value):
                self.set(segs + [str(i)], v, in_include, expand_alias)
            for i in reversed(range(len(value), n)):
                self.remove(segs + [str(i)])
            return
        self.replace(vn, value)

    def create(self, segs: list[str], parent: VNode, i: int, value: Any, in_include: bool = False) -> None:
        path = ".".join(segs)
        new = value
        for seg in reversed(segs[i + 1 :]):
            if seg.isdigit():
                if seg != "0":
                    raise PathError(f"{path}: index {seg} out of range")
                new = [new]
            else:
                new = {seg: new}
        seg = segs[i]
        if parent.kind == "map":
            if in_include:
                while parent.base is not None:  # the included side of a merged mapping
                    parent = parent.base
                self.insert(parent, seg, new)
            else:
                self.insert_key(parent, seg, new)
        elif parent.kind == "seq":
            if not seg.isdigit():
                raise PathError(f"{path}: {'.'.join(segs[:i]) or 'root'} is a list")
            if int(seg) != len(parent.children):
                raise PathError(f"{path}: index {seg} out of range")
            self.insert(parent, None, new)
        else:
            raise PathError(f"{path}: {'.'.join(segs[:i])} is not a mapping or list")

    def insert_key(self, parent: VNode, key: str, value: Any) -> None:
        """Add a new key; under included content it becomes an override in the including file."""
        include, rel, a = parent, [], parent
        while include is not None and not (
            include.include == "yaml" and include.include_doc is not None and not is_remote(include.src_doc.path)
        ):  # overrides of remote content go in the local file holding the include line
            include = include.parent
        if include is not None:
            while a is not include and (a.doc is include.include_doc or is_remote(a.doc.path)):  # climb to the nearest override
                p = a.parent
                if p.kind != "map":  # overrides cannot address list items
                    break
                rel.insert(0, next(k for k, c in p.children.items() if c is a))
                a = p
            else:
                for k in reversed(rel):
                    key, value = k, {key: value}
                if a.doc is include.include_doc:
                    self.to_mapping_form(a, {key: value})
                else:
                    self.insert(a, key, value)
                return
        self.insert(parent, key, value)

    def to_mapping_form(self, vn: VNode, overrides: dict[str, Any]) -> None:
        """Rewrite `key: file:x` as `key:` / `$ref: file:x` / overrides."""
        doc = vn.src_doc
        node = vn.src_node
        text = doc.text
        r = layout(doc)
        nl = doc.newline
        s, e = node.start_mark.index, node.end_mark.index
        ref = text[s:e]
        if "\n" in ref:
            ref = scalar(construct(node), None, False)
        if vn.flow_parent:
            entry = r.flow(overrides)
            self.apply(doc, [(s, e, "{$ref: " + ref + ", " + entry[1:], True)])
            return
        rest = text[e : doc.eol(e)]
        if vn.key_node is not None:
            col = vn.anchor_indent + r.step
            lines = [" " * col + "$ref: " + ref] + r.block(overrides, col)
            if doc.line_of(s) == doc.line_of(vn.anchor):
                edits = [(vn.anchor, doc.eol(e) if not rest.strip() else e, "", False)]
            else:  # value on its own line: replace that line
                edits = [(doc.line_start(s), doc.next_line(e), "", False)]
        else:  # a list item or the whole document
            col = vn.anchor_indent + 2 if vn.anchor is not None else 0
            lines = r.block(overrides, col)
            edits = [(s, e, "$ref: " + ref, False)]
        pos = doc.next_line(e)
        pre = nl if pos == len(text) and not text.endswith("\n") else ""
        edits.append((pos, pos, pre + _join(lines, nl), False))
        self.apply(doc, edits)

    @staticmethod
    def siblings(parent: VNode, container: Node | None) -> list[VNode]:
        """Entries of `parent` written in the same collection node, in file order."""
        out = [e for e in parent.entries() if e.container is container]
        if parent.ref_entry is not None and parent.ref_entry.container is container:
            out.append(parent.ref_entry)
        return sorted(out, key=lambda e: e.start)

    def remove(self, segs: list[str]) -> None:
        vn = self.load()
        for s in segs:
            vn = vn.child(s)
        parent = vn.parent
        assert parent is not None
        doc = vn.src_doc
        text = doc.text
        entries = self.siblings(parent, vn.container)
        idx = entries.index(vn)
        st, en = vn.start, vn.end
        if vn.container is not None and is_flow(vn.container):
            if idx < len(entries) - 1:
                edit = (st, entries[idx + 1].start, "", True)
            elif idx > 0:
                edit = (entries[idx - 1].end, en, "", True)
            else:
                edit = (st, en, "", True)
        else:
            ls = doc.line_start(st)
            if text[ls:st].strip() == "":
                edit = (ls, doc.next_line(en), "", False)
            elif idx < len(entries) - 1:
                edit = (st, entries[idx + 1].start, "", False)
            else:
                edit = (st, en, "", True)
        self.apply(doc, [edit])

    # insertion -------------------------------------------------------------
    def insert(self, parent: VNode, key: str | None, value: Any) -> None:
        doc = parent.doc
        node = parent.node
        r = layout(doc)
        entries = self.siblings(parent, node)
        last = entries[-1] if entries else None
        kstyle = style_of(last.key_node) if last is not None and last.key_node is not None else None
        vstyle = style_of(last.src_node) if last is not None else None
        if node is not None and is_flow(node):
            entry = r.flow(value, vstyle)
            if key is not None:
                entry = f"{scalar(key, kstyle, True)}: {entry}"
            if last is not None:
                self.apply(doc, [(last.end, last.end, ", " + entry, True)])
            else:
                p = node.start_mark.index + 1
                self.apply(doc, [(p, p, entry, True)])
            return
        text = doc.text
        nl = doc.newline
        if last is None:
            lines = r.entry(key, value, 0) if key is not None else r.item(value, 0)
            pre = nl if text and not text.endswith("\n") else ""
            self.apply(doc, [(len(text), len(text), pre + _join(lines, nl), False)])
            return
        col = doc.col(last.start)
        n = doc.line_of(last.end)
        k = n + 1
        while k < doc.nlines:
            line = doc.line_text(k)
            if not line.strip():
                k += 1
                continue
            if line.lstrip().startswith("#") and indent_of(line) >= col:
                n = k
                k += 1
                continue
            break
        if key is not None:
            lines = r.entry(key, value, col, kstyle, vstyle)
        else:
            lines = r.item(value, col, vstyle)
        pos = doc.next_line(doc.line_starts[n])
        pre = nl if pos == len(text) and text and not text.endswith("\n") else ""
        self.apply(doc, [(pos, pos, pre + _join(lines, nl), False)])

    # replacement -------------------------------------------------------------
    def replace(self, vn: VNode, value: Any) -> None:
        doc = vn.src_doc
        node = vn.src_node
        text = doc.text
        r = layout(doc)
        nl = doc.newline
        a = vn.anchor
        if vn.flow_parent:
            if is_empty_null(node) and a is not None:
                self.apply(doc, [(a, a, " " + r.flow(value), True)])
            else:
                self.apply(doc, [(value_start(vn), vn.end, r.flow(value, style_of(node)), True)])
            return
        if is_block_scalar(node) and isinstance(value, str) and not is_literal_tagged(node) and not isinstance(value, Literal):
            self.replace_block_scalar(vn, value, r)
            return
        assert a is not None
        compact = vn.key_node is None
        head, lines = r.value_after_indicator(value, vn.anchor_indent, style_of(node), compact)
        inline_old: tuple[int, int] | None = None
        region: tuple[int, int] | None = None
        if is_empty_null(node):
            pass
        elif isinstance(node, ScalarNode) and not is_block_scalar(node) or is_flow(node):
            s = node.start_mark.index
            if not lines and head:
                self.apply(doc, [(s, vn.end, head, True)])
                return
            if doc.line_of(s) == doc.line_of(a):
                inline_old = (s, vn.end)
            else:
                region = (doc.line_start(s), doc.next_line(vn.end))
        elif is_block_scalar(node):
            s = node.start_mark.index
            inline_old = (s, header_end(text, tag_end(text, s)))
            if vn.end > doc.eol(s):
                region = (doc.next_line(s), doc.next_line(vn.end))
        else:  # block collection
            s = node.start_mark.index
            if doc.line_of(s) == doc.line_of(a):
                inline_old = (s, doc.eol(s))
                if vn.end > doc.eol(s):
                    region = (doc.next_line(s), doc.next_line(vn.end))
            else:
                region = (doc.line_start(s), doc.next_line(vn.end))
        edits: list[Edit] = []
        if region is not None:
            final = text[region[1] - 1 : region[1]] == "\n"
            edits.append((region[0], region[1], _join(lines, nl, final), False))
        elif lines:
            after = inline_old[1] if inline_old else a
            pos = doc.next_line(after)
            pre = nl if pos == len(text) and not text.endswith("\n") else ""
            edits.append((pos, pos, pre + _join(lines, nl, True), False))
        if inline_old is not None and head:
            edits.append((inline_old[0], inline_old[1], head, True))
        elif inline_old is not None:
            edits.append((a, inline_old[1], "", True))
        elif head:
            edits.append((a, a, " " + head, True))
        self.apply(doc, edits)

    def replace_block_scalar(self, vn: VNode, value: str, r: Renderer) -> None:
        doc = vn.src_doc
        node = vn.src_node
        text = doc.text
        nl = doc.newline
        s = node.start_mark.index
        he = header_end(text, s)
        indicator, mods = text[s], text[s + 1 : he]
        old_chomp = "+" if "+" in mods else "-" if "-" in mods else ""
        digits = "".join(c for c in mods if c.isdigit())
        cstart = doc.next_line(s)
        if vn.end > doc.eol(s):
            rend = doc.next_line(vn.end)
        else:
            rend = cstart
        old_lines = _split_lines(text[cstart:rend])
        new_chomp = chomp_for(value, old_chomp)
        trailing: list[str] = []
        if old_chomp == "+" or new_chomp == "+":
            while rend < len(text) and not text[rend : doc.eol(rend)].strip():
                trailing.append(text[rend : doc.eol(rend)])
                rend = doc.next_line(rend)
        first = next((ln for ln in old_lines if ln.strip()), None)
        if first is not None:
            ci = indent_of(first)
        elif digits:
            ci = vn.anchor_indent + int(digits)
        else:
            ci = vn.anchor_indent + r.step
        body = value.rstrip("\n")
        t = len(value) - len(body)

        if indicator == "|":
            ok = literal_ok(value) or (digits and literal_ok(value.lstrip(" ")))
            if not ok:
                self.replace_with_inline(vn, double(value))
                return
            new_texts = body.split("\n")
            old_texts = [ln[ci:] if len(ln) > ci else "" for ln in old_lines]
            out: list[str] = []
            sm = difflib.SequenceMatcher(None, old_texts, new_texts, autojunk=False)
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op == "equal":
                    out += old_lines[i1:i2]
                else:
                    out += [(" " * ci + x) if x else "" for x in new_texts[j1:j2]]
        else:
            out = self.folded_lines(value, body, old_lines, ci)

        if new_chomp == "+":
            out += trailing if len(trailing) == t - 1 else [""] * (t - 1)
        elif trailing:
            # old keep-chomped blank lines that are no longer content
            out += trailing

        if old_chomp:
            new_mods = mods.replace(old_chomp, new_chomp)
        else:
            new_mods = mods + new_chomp
        if cstart == len(text) and not text.endswith("\n"):
            chunk = nl + _join(out, nl, False) if out else ""
        else:
            chunk = _join(out, nl, rend < len(text) or text.endswith("\n"))
        edits: list[Edit] = [(cstart, rend, chunk, False)]
        edits.append((s, he, indicator + new_mods, True))
        self.apply(doc, edits)

    def replace_with_inline(self, vn: VNode, head: str) -> None:
        doc = vn.src_doc
        node = vn.src_node
        s = node.start_mark.index
        edits: list[Edit] = []
        if vn.end > doc.eol(s):
            edits.append((doc.next_line(s), doc.next_line(vn.end), "", False))
        edits.append((s, header_end(doc.text, s), head, True))
        self.apply(doc, edits)

    @staticmethod
    def folded_lines(value: str, body: str, old_lines: list[str], ci: int) -> list[str]:
        # old structure
        lead: list[str] = []
        paras: list[list[str]] = []
        seps: list[list[str]] = []
        blanks: list[str] = []
        for ln in old_lines:
            if not ln.strip():
                blanks.append(ln)
                continue
            if paras and not blanks:
                paras[-1].append(ln)
            else:
                if paras:
                    seps.append(blanks)
                else:
                    lead = blanks
                paras.append([ln])
            blanks = []
        old_texts = [" ".join(ln[ci:] for ln in p) for p in paras]
        widths = [max(len(ln) - ci for ln in p) for p in paras]
        width_all = max(widths) if widths else 80

        stripped = body.lstrip("\n")
        n_lead = len(body) - len(stripped)
        new_paras = re.split(r"\n+", stripped) if stripped else []
        new_seps = [len(m) for m in re.findall(r"\n+", stripped)]
        for p in new_paras:
            if p[:1] in (" ", "\t"):
                raise WriteRefused("value would put a more-indented line into a folded scalar")

        cp: list[int | None] = [None] * len(new_paras)
        same: list[bool] = [False] * len(new_paras)
        sm = difflib.SequenceMatcher(None, old_texts, new_paras, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                for k in range(i2 - i1):
                    cp[j1 + k] = i1 + k
                    same[j1 + k] = True
            elif op == "replace":
                for k in range(min(i2 - i1, j2 - j1)):
                    cp[j1 + k] = i1 + k

        out: list[str] = list(lead) if len(lead) == n_lead else [""] * n_lead
        for j, p in enumerate(new_paras):
            i = cp[j]
            if same[j]:
                out += paras[i]
            else:
                w = widths[i] if i is not None else width_all
                out += [" " * ci + ln for ln in wrap(p, w)]
            if j < len(new_seps):
                k = new_seps[j]
                nxt = cp[j + 1]
                if i is not None and nxt == i + 1 and len(seps[i]) == k:
                    out += seps[i]
                else:
                    out += [""] * k
        return out
