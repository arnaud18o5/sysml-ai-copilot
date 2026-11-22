"""Deterministic model operations: the API an agent (or the UI) drives.

Every public method of `ModelService` takes plain JSON-compatible arguments
and returns a plain dict. Nothing here guesses: an operation either applies
exactly what was asked and reports what changed, or refuses with a
structured error (`code`, `message`, `suggestions`) and leaves the model
untouched. This is what makes the tool usable by small models: they only
have to pick the right operation and arguments, the tool guarantees the
result is a well-formed model.

Mutations:
  * are transactional (`apply_batch` is all-or-nothing),
  * keep references consistent (renaming or moving an element rewrites
    every reference to it),
  * refuse to leave dangling references (`delete_element` needs
    `cascade=True` to remove dependents),
  * are undoable (`undo` / `redo`), and saved to disk with `save`
    (automatically when the service runs with `autosave=True`).
"""

from __future__ import annotations

import difflib
import os
import re
from collections import deque

from .model import (
    DIRECTIONS,
    ELEMENT_KEYWORDS,
    KEYWORD_BY_KIND,
    KIND_BY_KEYWORD,
    RELATIONSHIP_KINDS,
    COMPATIBLE_TYPING,
    Document,
    Element,
    Import,
    Ref,
    Relationship,
    Value,
    format_path,
)
from .syntax import (
    SysmlSyntaxError,
    parse_document,
    parse_ref,
    parse_value,
    render_statement,
    serialize_document,
    serialize_node,
    element_header,
)
from . import config
from .semantic import SemanticIndex, SemanticUnavailable
from .workspace import HOP_INHERITED, HOP_MEMBER, HOP_SCOPE, Workspace, _expand

# Relation types followed by impact analysis. CONTAINS is deliberately
# excluded (see CLAUDE.md): it would flood results with unrelated siblings.
IMPACT_RELATION_TYPES = ("TYPED_BY", "SPECIALIZES", "SUBSETS", "REDEFINES",
                         "CONNECTS_TO", "SATISFIES", "ALLOCATES")

# For "dependents" impact (what is affected if X changes): relations whose
# *source* depends on their *target* are followed backwards only; the others
# propagate both ways.
_DEPENDENCY_RELATIONS = {"TYPED_BY", "SPECIALIZES", "SUBSETS", "REDEFINES"}

MUTATING_OPERATIONS = (
    "create_element", "update_element", "delete_element", "move_element",
    "add_relationship", "remove_relationship", "add_import", "remove_import",
    "add_from_text", "create_document",
)

_MULTIPLICITY = re.compile(r"^(\d+|\*)(\.\.(\d+|\*))?$")


class OpError(Exception):
    def __init__(self, code, message, suggestions=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []
        self.details = details

    def to_json(self):
        data = {"code": self.code, "message": self.message}
        if self.suggestions:
            data["suggestions"] = self.suggestions
        if self.details is not None:
            data["details"] = self.details
        return data


def normalize_kind(kind: str):
    """'part def' / 'PartDefinition' / 'part_def' / 'part' -> (keyword, is_definition)."""
    if not isinstance(kind, str) or not kind.strip():
        raise OpError("invalid_kind", "kind must be a non-empty string", suggestions=supported_kinds())
    raw = kind.strip()
    if raw in KEYWORD_BY_KIND:
        return KEYWORD_BY_KIND[raw]
    text = re.sub(r"[_\-]+", " ", raw).lower().strip()
    text = re.sub(r"\s+", " ", text)
    if text == "package":
        return "package", False
    for suffix in (" definition", " def"):
        if text.endswith(suffix):
            kw = text[: -len(suffix)].strip()
            kw = {"verification case": "verification", "analysis case": "analysis",
                  "enumeration": "enum", "calculation": "calc"}.get(kw, kw)
            if kw in ELEMENT_KEYWORDS:
                return kw, True
    for suffix in (" usage",):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    text = {"verification case": "verification", "analysis case": "analysis",
            "enumeration": "enum", "calculation": "calc"}.get(text, text)
    if text in ELEMENT_KEYWORDS:
        return text, False
    close = difflib.get_close_matches(raw.lower(), supported_kinds(), n=3, cutoff=0.5)
    raise OpError("invalid_kind", f"unknown element kind '{kind}'", suggestions=close or supported_kinds())


def supported_kinds():
    out = ["package"]
    for kw in ELEMENT_KEYWORDS:
        out.append(f"{kw} def")
        out.append(kw)
    return out


def _value_from_input(value, value_kind=None) -> Value:
    """Build a Value from agent input. See `create_element` docs for rules."""
    if value_kind not in (None, "auto", "number", "string", "boolean", "ref", "expression"):
        raise OpError("invalid_value_kind", f"unknown value_kind '{value_kind}'",
                      suggestions=["auto", "number", "string", "boolean", "ref", "expression"])
    if isinstance(value, bool):
        if value_kind not in (None, "auto", "boolean"):
            raise OpError("invalid_value", f"boolean value given with value_kind '{value_kind}'")
        return Value("boolean", "true" if value else "false")
    if isinstance(value, (int, float)):
        if value_kind not in (None, "auto", "number"):
            raise OpError("invalid_value", f"numeric value given with value_kind '{value_kind}'")
        return Value("number", repr(value) if isinstance(value, float) else str(value))
    if not isinstance(value, str):
        raise OpError("invalid_value", "value must be a number, boolean or string")
    if value_kind == "string":
        return Value("string", value)
    if value_kind in ("ref", "expression", "number", "boolean"):
        try:
            v = parse_value(value)
        except SysmlSyntaxError as exc:
            raise OpError("invalid_value", f"cannot parse value '{value}': {exc}") from exc
        if value_kind != "expression" and v.kind != value_kind:
            raise OpError("invalid_value", f"'{value}' is a {v.kind}, not a {value_kind}")
        return v
    # auto: numbers (with optional unit), booleans and quoted strings are
    # parsed; anything else is a plain string literal.
    stripped = value.strip()
    if re.match(r'^[+-]?(\d|\.\d)', stripped) or stripped in ("true", "false") or stripped.startswith('"'):
        try:
            v = parse_value(stripped)
            if v.kind != "ref":
                return v
        except SysmlSyntaxError:
            pass
    return Value("string", value)


class ModelService:
    """Holds a workspace and exposes deterministic operations on it."""

    def __init__(self, paths=None, autosave=False, workspace: Workspace | None = None,
                 semantic: SemanticIndex | None = None):
        self.paths = list(paths or [])
        self.semantic = semantic or SemanticIndex()
        self.autosave = autosave
        if workspace is not None:
            self.ws = workspace
        elif self.paths:
            self.ws = Workspace.load(*self.paths)
        else:
            self.ws = Workspace()
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._in_tx = False
        self._disk = {d.path: _read(d.path) for d in self.ws.documents}
        self._unsaved = False
        self.version = 0  # bumped on every change of the in-memory model
        self._stamps = self._file_stamps()

    # ========================================================= disk syncing

    def _tracked_files(self):
        files = {d.path for d in self.ws.documents if not d.path.startswith("<")}
        for p in self.paths:
            if os.path.isdir(p):
                files.update(_expand(p))
        return sorted(files)

    def _file_stamps(self):
        stamps = {}
        for f in self._tracked_files():
            try:
                st = os.stat(f)
                stamps[f] = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamps[f] = None
        return stamps

    def sync(self, for_write=False):
        """Pick up changes made to the files by someone else (another process, an
        editor, `git pull`). Reloads when this service has no unsaved changes;
        otherwise refuses writes with a `conflict` error."""
        stamps = self._file_stamps()
        if stamps == self._stamps:
            return
        changed = [f for f in stamps if _read(f) != self._disk.get(f)]
        if not changed:
            self._stamps = stamps
            return
        if self._unsaved:
            if for_write:
                raise OpError("conflict",
                              f"{len(changed)} file(s) changed on disk while this session has unsaved changes; "
                              f"call diff to inspect, then reload (discards unsaved changes) or save",
                              details=changed)
            return
        try:
            files = [f for f in stamps if stamps[f] is not None]
            ws = Workspace.load(*files) if files else Workspace()
        except SysmlSyntaxError as exc:
            if for_write:
                raise OpError("conflict", f"a file changed on disk and no longer parses: {exc}",
                              details=exc.to_json()) from exc
            return
        self.ws = ws
        self._disk = {d.path: _read(d.path) for d in ws.documents}
        self._stamps = stamps
        self._undo.clear()
        self._redo.clear()
        self.version += 1

    # ================================================================ helpers

    def _snapshot(self):
        return {"docs": [(d.path, serialize_document(d)) for d in self.ws.documents]}

    def _restore(self, snap):
        ws = Workspace()
        for path, text in snap["docs"]:
            ws.documents.append(parse_document(text, path))
        ws.reindex()
        self.ws = ws

    def _counts(self):
        diags = self.ws.diagnostics()
        return {
            "errors": sum(d.severity == "error" for d in diags),
            "warnings": sum(d.severity == "warning" for d in diags),
        }

    def _mutate(self, label, fn):
        if self._in_tx:
            return fn()
        snap = self._snapshot()
        self._in_tx = True
        try:
            result = fn()
        except OpError:
            self._restore(snap)
            raise
        except Exception as exc:  # never leave a half-applied change behind
            self._restore(snap)
            raise OpError("internal_error", f"{type(exc).__name__}: {exc}") from exc
        finally:
            self._in_tx = False
        if self._snapshot() != snap:
            self._undo.append({"label": label, "snap": snap})
            self._redo.clear()
            self.version += 1
            self._unsaved = True
            if self.autosave:
                result["saved"] = self._save()
        result.setdefault("ok", True)
        result["validation"] = self._counts()
        return result

    def _capture_refs(self):
        """Resolution of every reference, to re-anchor them after a change."""
        captured = []
        for node, role, ref in self.ws.all_refs():
            res = self.ws.resolve_node_ref(node, ref)
            if res.ok and not isinstance(node, Import):
                captured.append((node, role, ref, res.path, res.hops))
        return captured

    def _reanchor(self, captured, removed=()):
        """Rewrite references whose meaning changed after a rename/move."""
        removed = {id(e) for e in removed}
        self.ws.reindex()
        alive = {id(n) for n in self.ws.nodes()}
        pending = [c for c in captured
                   if id(c[0]) in alive and not any(id(e) in removed for e in c[3])]
        rewritten = {}
        # References can depend on each other (a feature chain resolves
        # through a typing that is itself being rewritten), so iterate until
        # no further progress is made.
        while True:
            still, progress = [], False
            for entry in pending:
                node, role, ref, path, hops = entry
                res = self.ws.resolve_node_ref(node, ref)
                same = res.ok and len(res.path) == len(path) and all(a is b for a, b in zip(res.path, path))
                # A reference that now only resolves by the global-name fallback is rewritten too.
                if same and (not res.heuristic or hops[0] == "heuristic"):
                    continue
                new = self.ws.ref_for(path, hops, node.owner, node.document)
                if new is None:
                    still.append(entry)
                    continue
                key = id(ref)
                old = rewritten[key]["from"] if key in rewritten else str(ref)
                ref.segments[:] = new.segments
                rewritten[key] = {"in": self._label(node), "role": role, "from": old, "to": str(ref)}
                progress = True
                self.ws.reindex()
            pending = still
            if not pending or not progress:
                break
        broken = [{"in": self._label(n), "role": r, "ref": str(ref)} for n, r, ref, _, _ in pending]
        self.ws.reindex()
        return list(rewritten.values()), broken

    def _label(self, node):
        if isinstance(node, Element):
            return self.ws.id_of(node)
        owner = self.ws.id_of(node.owner) if node.owner else node.document.path
        return f"{owner}: {render_statement(node)}"

    def _resolve_path(self, text, what="element"):
        if not isinstance(text, str) or not text.strip():
            raise OpError("invalid_argument", f"{what} must be a non-empty element id")
        res = self.ws.lookup(text.strip())
        if not res.ok:
            try:
                segs = parse_ref(text).segments
                missing = segs[res.failed_segment or 0]
            except SysmlSyntaxError:
                missing = text
            sugg = self.ws.suggest(missing)
            raise OpError("not_found", f"no {what} '{text}' ('{missing}' not found)", suggestions=sugg)
        return res

    def _element(self, text, what="element", allow_library=False, allow_chain=False) -> Element:
        res = self._resolve_path(text, what)
        if not allow_chain and HOP_INHERITED in res.hops[1:]:
            canonical = self.ws.id_of(res.target)
            raise OpError(
                "inherited_feature",
                f"'{text}' is inherited: it is declared on another element as {canonical}. "
                f"Use that id to edit it (the change affects every usage of its owner).",
                suggestions=[canonical])
        el = res.target
        if el.library and not allow_library:
            raise OpError("read_only", f"{self.ws.id_of(el)} is a built-in library element and cannot be modified")
        return el

    def _type_ref(self, owner_scope, document, text, usage_keyword, what="type"):
        target = self._element(text, what, allow_library=True)
        if not target.is_definition:
            defs = [self.ws.id_of(e) for e in self.ws.elements(include_library=True)
                    if e.is_definition and e.effective_name == target.effective_name]
            raise OpError("not_a_definition",
                          f"{self.ws.id_of(target)} is a {target.kind}; a {what} must be a definition",
                          suggestions=defs)
        if usage_keyword is not None:
            allowed = COMPATIBLE_TYPING.get(usage_keyword, {usage_keyword})
            if target.keyword not in allowed:
                raise OpError(
                    "incompatible_type",
                    f"a {usage_keyword} cannot be typed by {self.ws.id_of(target)} ({target.kind}); "
                    f"expected a definition of kind: {', '.join(sorted(k + ' def' for k in allowed))}")
        return self.ws.ref_to(target, owner_scope, document)

    def _check_name(self, name, owner, document, exclude=None):
        if not isinstance(name, str) or not name.strip() or "\n" in name:
            raise OpError("invalid_name", "name must be a non-empty single-line string")
        siblings = owner.members if owner is not None else document.members
        for m in siblings:
            if isinstance(m, Element) and m is not exclude and m.effective_name == name:
                raise OpError("duplicate_name",
                              f"{self.ws.id_of(m)} already exists", suggestions=[self.ws.id_of(m)])

    def _document_for_root(self, document):
        if document is None:
            if len(self.ws.documents) == 1:
                return self.ws.documents[0]
            raise OpError("document_required",
                          "owner is empty (top level) and there are several documents; pass 'document'",
                          suggestions=[d.path for d in self.ws.documents])
        doc = self.ws.document(document)
        if doc is None:
            raise OpError("not_found", f"no document '{document}'",
                          suggestions=[d.path for d in self.ws.documents])
        return doc

    @staticmethod
    def _insert(container_members, node, index=None):
        if index is not None:
            if not isinstance(index, int) or index < 0:
                raise OpError("invalid_argument", "index must be a non-negative integer")
            container_members.insert(min(index, len(container_members)), node)
            return
        if isinstance(node, Import):
            pos = 0
            while pos < len(container_members) and isinstance(container_members[pos], Import):
                pos += 1
            container_members.insert(pos, node)
            return
        if isinstance(node, Element):
            pos = len(container_members)
            while pos > 0 and isinstance(container_members[pos - 1], Relationship):
                pos -= 1
            container_members.insert(pos, node)
            return
        container_members.append(node)

    @staticmethod
    def _set_document(node, document):
        node.document = document
        if isinstance(node, Element):
            for n in node.walk():
                n.document = document

    def _chain_id(self, path):
        head = path[0]
        names = head.path_names()[:-1] + [e.effective_name for e in path]
        return format_path(names)

    # ================================================================ reading

    def summary(self) -> dict:
        """Overview: documents, top-level packages, element counts per kind, validation counts."""
        counts = {}
        for el in self.ws.elements():
            counts[el.kind] = counts.get(el.kind, 0) + 1
        rels = {}
        for e in self.ws.edges():
            rels[e.type] = rels.get(e.type, 0) + 1
        return {
            "documents": [{"path": d.path,
                           "roots": [self.ws.id_of(m) for m in d.members if isinstance(m, Element)],
                           "unsaved_changes": serialize_document(d) != self._disk.get(d.path)}
                          for d in self.ws.documents],
            "element_count": sum(counts.values()),
            "elements_by_kind": dict(sorted(counts.items())),
            "relations_by_type": dict(sorted(rels.items())),
            "validation": self._counts(),
            "can_undo": bool(self._undo),
            "can_redo": bool(self._redo),
            "unsaved_changes": self._unsaved,
            "version": self.version,
        }

    def _brief(self, el: Element):
        data = {"id": self.ws.id_of(el), "kind": el.kind, "name": el.effective_name}
        if el.types:
            data["type"] = ", ".join(str(r) for r in el.types)
        return data

    def list_elements(self, kind=None, owner=None, recursive=True, name_contains=None, limit=200) -> dict:
        """List elements, optionally filtered by kind, owner (subtree) and name substring."""
        kw = normalize_kind(kind) if kind else None
        if owner:
            root = self._element(owner, "owner", allow_library=True)
            pool = [e for e in root.walk() if isinstance(e, Element) and e is not root]
            if not recursive:
                pool = root.element_members()
        else:
            pool = list(self.ws.elements())
        out = []
        for el in pool:
            if kw and (el.keyword, el.is_definition) != kw:
                continue
            if name_contains and name_contains.lower() not in (el.effective_name or "").lower():
                continue
            out.append(self._brief(el))
        total = len(out)
        return {"total": total, "elements": out[:limit], "truncated": total > limit}

    def get_element(self, element, include_source=True) -> dict:
        """Full details of one element: properties, members, inherited features, relations."""
        res = self._resolve_path(element)
        el = res.target
        ws = self.ws
        scope = el.owner
        data = {
            "id": ws.id_of(el),
            "kind": el.kind,
            "name": el.effective_name,
            "owner": ws.id_of(el.owner) if el.owner else None,
            "document": el.document.path,
            "library": el.library,
        }
        if HOP_INHERITED in res.hops[1:]:
            data["reached_via"] = self._chain_id(res.path)
        for flag in ("abstract",):
            if getattr(el, flag):
                data[flag] = True
        if el.direction:
            data["direction"] = el.direction
        if el.multiplicity:
            data["multiplicity"] = el.multiplicity
        if el.doc is not None:
            data["doc"] = el.doc

        def refs_json(refs):
            out = []
            for r in refs:
                rr = ws.resolve_node_ref(el, r)
                out.append({"ref": str(r), "id": ws.id_of(rr.target) if rr.ok else None})
            return out

        if el.types:
            data["types"] = refs_json(el.types)
        if el.specializes:
            data["specializes" if el.is_definition else "subsets"] = refs_json(el.specializes)
        if el.redefines:
            data["redefines"] = refs_json(el.redefines)
        if el.value is not None:
            data["value"] = el.value.to_json()
            if el.value.ref is not None:
                rr = ws.resolve_node_ref(el, el.value.ref)
                data["value"]["id"] = ws.id_of(rr.target) if rr.ok else None
        data["members"] = [self._brief(m) for m in el.element_members()]
        inherited = self._inherited_features(el)
        if inherited:
            data["inherited_features"] = inherited
        statements = []
        for m in el.members:
            if isinstance(m, Relationship):
                statements.append(self._statement_json(m))
            elif isinstance(m, Import):
                statements.append({"kind": "import", "path": m.render_path(), "text": render_statement(m)})
        if statements:
            data["statements"] = statements
        out_rel, in_rel = [], []
        for e in ws.edges():
            if e.type == "CONTAINS":
                continue
            if e.source is el:
                out_rel.append({"type": e.type, "target": ws.id_of(e.target),
                                **({"target_chain": self._chain_id(e.target_path)} if e.target_path and len(e.target_path) > 1 else {})})
            if e.target is el:
                in_rel.append({"type": e.type, "source": ws.id_of(e.source),
                               **({"source_chain": self._chain_id(e.source_path)} if e.source_path and len(e.source_path) > 1 else {})})
        data["relations"] = {"outgoing": out_rel, "incoming": in_rel}
        diags = [d.to_json() for d in ws.diagnostics() if d.element == data["id"]]
        if diags:
            data["diagnostics"] = diags
        if include_source and not el.library:
            data["source"] = serialize_node(el)
        return data

    def _inherited_features(self, el):
        out, seen = [], {m.effective_name for m in el.element_members()}
        stack = [(el, r) for r in [*el.types, *el.specializes]]
        visited = set()
        while stack:
            holder, ref = stack.pop(0)
            res = self.ws.resolve_node_ref(holder, ref)
            if not res.ok or id(res.target) in visited:
                continue
            sup = res.target
            visited.add(id(sup))
            for m in sup.element_members():
                if m.effective_name in seen:
                    continue
                seen.add(m.effective_name)
                out.append({"name": m.effective_name, "kind": m.kind, "declared_in": self.ws.id_of(sup),
                            "id": self.ws.id_of(m),
                            "chain_id": f"{self.ws.id_of(el)}.{format_path([m.effective_name])}"})
            stack.extend((sup, r) for r in [*sup.types, *sup.specializes])
        return out

    def _statement_json(self, node: Relationship):
        ws = self.ws
        s = ws.resolve_node_ref(node, node.source)
        t = ws.resolve_node_ref(node, node.target)
        return {
            "kind": node.kind,
            "type": node.relation_type,
            "owner": ws.id_of(node.owner) if node.owner else None,
            "source": self._chain_id(s.path) if s.ok else None,
            "target": self._chain_id(t.path) if t.ok else None,
            "text": render_statement(node),
            **({} if s.ok and t.ok else {"unresolved": True}),
        }

    def get_tree(self, root=None, depth=3) -> dict:
        """Containment tree from `root` (or all documents), `depth` levels deep."""
        def build(el, d):
            node = self._brief(el)
            members = el.element_members()
            stmts = [m for m in el.members if not isinstance(m, Element)]
            if stmts:
                node["statements"] = [render_statement(s) for s in stmts]
            if members:
                if d <= 0:
                    node["children_count"] = len(members)
                else:
                    node["children"] = [build(m, d - 1) for m in members]
            return node
        if root:
            el = self._element(root, "root", allow_library=True)
            return {"tree": build(el, depth)}
        return {"documents": [{"path": doc.path,
                               "roots": [build(m, depth - 1) for m in doc.members if isinstance(m, Element)]}
                              for doc in self.ws.documents]}

    def find_elements(self, query, kind=None, limit=10, mode="auto") -> dict:
        """Search elements by meaning and by name.

        mode: "lexical" (names camelCase-aware + doc words), "semantic" (multilingual
        embeddings of kind/name/owner/type/doc: 'ce qui alimente le moteur' finds FuelPump),
        or "auto" (both, ranked by the best of the two scores - an exact name match always
        ranks first; lexical only if the embedding model is unavailable)."""
        if not isinstance(query, str) or not query.strip():
            raise OpError("invalid_argument", "query must be a non-empty string")
        if mode not in ("auto", "lexical", "semantic"):
            raise OpError("invalid_argument", "mode must be 'auto', 'lexical' or 'semantic'")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise OpError("invalid_argument", "limit must be an integer between 1 and 100")
        kw = normalize_kind(kind) if kind else None
        pool = [el for el in self.ws.elements(include_library=False)
                if not kw or (el.keyword, el.is_definition) == kw]
        scores: dict[int, dict] = {}
        if mode in ("auto", "lexical"):
            for score, el in self._lexical_scores(query, pool):
                scores.setdefault(id(el), {"el": el})["lexical_score"] = score
        used = "lexical"
        note = None
        if mode in ("auto", "semantic") and (mode == "semantic" or config.SEMANTIC_SEARCH != "off"):
            try:
                for cos, el in self.semantic.search(self.ws, query, pool, limit=max(limit, 20)):
                    scores.setdefault(id(el), {"el": el})["semantic_score"] = round(cos, 3)
                used = "semantic" if mode == "semantic" else "hybrid"
            except SemanticUnavailable as exc:
                if mode == "semantic":
                    raise OpError("semantic_unavailable", str(exc)) from exc
                note = f"semantic search unavailable, lexical results only: {exc}"
        rows = []
        for entry in scores.values():
            el = entry.pop("el")
            # Semantic similarity maps to at most 95, so an exact name match (100) always ranks first.
            best = max(entry.get("lexical_score", 0.0), 95.0 * entry.get("semantic_score", 0.0))
            rows.append((round(best, 1), self.ws.id_of(el), el, entry))
        rows.sort(key=lambda t: (-t[0], t[1]))
        out = {"mode": used, "results": [{**self._brief(el), "score": s, **extra}
                                         for s, _, el, extra in rows[:limit]]}
        if note:
            out["note"] = note
        return out

    def _lexical_scores(self, query, pool):
        q = _normalize_words(query)
        q_tokens = set(q.split())
        out = []
        for el in pool:
            name = el.effective_name or ""
            n = _normalize_words(name)
            if name.lower() == query.strip().lower() or n == q:
                score = 100.0
            elif n.startswith(q):
                score = 80.0
            elif q in n:
                score = 60.0
            else:
                score = 50.0 * difflib.SequenceMatcher(None, q, n).ratio()
                n_tokens = set(n.split())
                if q_tokens & n_tokens:
                    score = max(score, 40.0 + 10.0 * len(q_tokens & n_tokens))
            if el.doc:
                d_tokens = set(_normalize_words(el.doc).split())
                score += 8.0 * len(q_tokens & d_tokens)
            if q_tokens & {el.keyword, "definition" if el.is_definition else "usage"}:
                score += 5.0
            if score >= 25:
                out.append((round(score, 1), el))
        return out

    def list_relationships(self, element=None, type=None, include_containment=False) -> dict:
        """Derived relations (TYPED_BY, SPECIALIZES, CONNECTS_TO, SATISFIES, ...)."""
        el = self._resolve_path(element).target if element else None
        rtype = type.upper() if type else None
        out = []
        for e in self.ws.edges():
            if e.type == "CONTAINS" and not include_containment and rtype != "CONTAINS":
                continue
            if rtype and e.type != rtype:
                continue
            if el is not None and el is not e.source and el is not e.target:
                continue
            item = {"type": e.type, "source": self.ws.id_of(e.source), "target": self.ws.id_of(e.target)}
            if e.source_path and len(e.source_path) > 1:
                item["source_chain"] = self._chain_id(e.source_path)
            if e.target_path and len(e.target_path) > 1:
                item["target_chain"] = self._chain_id(e.target_path)
            if isinstance(e.origin, Relationship):
                item["declared_in"] = self.ws.id_of(e.origin.owner) if e.origin.owner else e.origin.document.path
            out.append(item)
        return {"total": len(out), "relations": out}

    def impact_analysis(self, element, max_hops=3, direction="dependents", relation_types=None) -> dict:
        """Elements affected by a change to `element`, with the path explaining each one.

        direction: "dependents" (what is affected if the element changes; default),
        "dependencies" (what the element relies on) or "both" (undirected).
        """
        if not isinstance(max_hops, int) or isinstance(max_hops, bool) or not 1 <= max_hops <= 10:
            raise OpError("invalid_argument", "max_hops must be an integer between 1 and 10")
        if direction not in ("dependents", "dependencies", "both"):
            raise OpError("invalid_argument", "direction must be 'dependents', 'dependencies' or 'both'")
        types = set(IMPACT_RELATION_TYPES)
        if relation_types:
            bad = [t for t in relation_types if t.upper() not in IMPACT_RELATION_TYPES + ("CONTAINS",)]
            if bad:
                raise OpError("invalid_argument", f"unknown relation types {bad}",
                              suggestions=list(IMPACT_RELATION_TYPES))
            types = {t.upper() for t in relation_types}
        start = self._resolve_path(element).target
        adj: dict[int, list] = {}

        def link(a, b, rel, label):
            adj.setdefault(id(a), []).append((b, rel, label))

        for e in self.ws.edges():
            if e.type not in types:
                continue
            pairs = [(e.source, e.target)]
            if e.type == "CONNECTS_TO" and e.source_path and e.target_path:
                pairs.append((e.source_path[0], e.target_path[0]))  # the connected parts
            for a, b in pairs:
                forward = direction in ("dependencies", "both") or e.type not in _DEPENDENCY_RELATIONS
                backward = direction in ("dependents", "both") or e.type not in _DEPENDENCY_RELATIONS
                if direction == "dependencies" and e.type not in _DEPENDENCY_RELATIONS:
                    forward = backward = True
                if forward:
                    link(a, b, e.type, "->")
                if backward:
                    link(b, a, e.type, "<-")
        seen = {id(start): None}
        order = []
        queue = deque([(start, 0)])
        while queue:
            cur, dist = queue.popleft()
            if dist >= max_hops:
                continue
            for nxt, rel, arrow in sorted(adj.get(id(cur), []), key=lambda t: (self.ws.id_of(t[0]), t[1])):
                if id(nxt) in seen:
                    continue
                seen[id(nxt)] = (cur, rel, arrow)
                order.append((nxt, dist + 1))
                queue.append((nxt, dist + 1))
        results = []
        for el, dist in order:
            via, cur = [], el
            while seen[id(cur)] is not None:
                prev, rel, arrow = seen[id(cur)]
                via.append(f"{self.ws.id_of(prev)} {'-' + rel + '->' if arrow == '->' else '<-' + rel + '-'} "
                           f"{self.ws.id_of(cur)}")
                cur = prev
            results.append({**self._brief(el), "distance": dist, "because": list(reversed(via))})
        return {"element": self.ws.id_of(start), "direction": direction, "max_hops": max_hops,
                "impacted": results}

    def requirements_trace(self) -> dict:
        """Requirement coverage: each requirement with the elements satisfying it."""
        reqs = [e for e in self.ws.elements() if e.keyword == "requirement"]
        rows = []
        for r in reqs:
            by = [self._chain_id(e.source_path) if e.source_path else self.ws.id_of(e.source)
                  for e in self.ws.edges() if e.type == "SATISFIES" and e.target is r]
            rows.append({"id": self.ws.id_of(r), "kind": r.kind, "doc": r.doc, "satisfied_by": by})
        covered = sum(1 for row in rows if row["satisfied_by"])
        return {"requirements": rows, "total": len(rows), "satisfied": covered,
                "coverage": round(covered / len(rows), 3) if rows else None}

    def validate(self, severity=None) -> dict:
        """Diagnostics: unresolved/ambiguous references, typing errors, unsatisfied requirements..."""
        levels = {"error": 0, "warning": 1, "info": 2}
        if severity is not None and severity not in levels:
            raise OpError("invalid_argument", "severity must be 'error', 'warning' or 'info'")
        diags = [d for d in self.ws.diagnostics()
                 if severity is None or levels[d.severity] <= levels[severity]]
        return {**self._counts(), "infos": sum(d.severity == "info" for d in self.ws.diagnostics()),
                "diagnostics": [d.to_json() for d in diags]}

    def get_source(self, element=None, document=None) -> dict:
        """SysML v2 text of an element, of a document, or of the whole workspace."""
        if element:
            el = self._element(element, allow_library=True, allow_chain=True)
            return {"id": self.ws.id_of(el), "source": serialize_node(el)}
        docs = [self._document_for_root(document)] if document else self.ws.documents
        return {"documents": [{"path": d.path, "source": serialize_document(d)} for d in docs]}

    def export_model(self) -> dict:
        """The whole model as JSON (elements, statements, relations, diagnostics) in one call."""
        ws = self.ws
        elements = []
        for el in ws.elements():
            def ids(refs):
                out = []
                for r in refs:
                    rr = ws.resolve_node_ref(el, r)
                    out.append(ws.id_of(rr.target) if rr.ok else None)
                return out
            item = {"id": ws.id_of(el), "kind": el.kind, "keyword": el.keyword,
                    "is_definition": el.is_definition, "name": el.effective_name,
                    "owner": ws.id_of(el.owner) if el.owner else None, "document": el.document.path,
                    "members": [ws.id_of(m) for m in el.element_members()]}
            for key in ("doc", "multiplicity", "direction"):
                if getattr(el, key) is not None:
                    item[key] = getattr(el, key)
            if el.abstract:
                item["abstract"] = True
            if el.types:
                item["types"] = ids(el.types)
                item["type_refs"] = [str(r) for r in el.types]
            if el.specializes:
                item["specializes"] = ids(el.specializes)
            if el.redefines:
                item["redefines"] = ids(el.redefines)
            if el.value is not None:
                item["value"] = el.value.to_json()
                item["value_text"] = el.value.render()
            inherited = self._inherited_features(el)
            if inherited:
                item["inherited_features"] = inherited
            elements.append(item)
        statements = [self._statement_json(n) for n in ws.nodes() if isinstance(n, Relationship)]
        return {
            "documents": [{"path": d.path, "roots": [ws.id_of(m) for m in d.members if isinstance(m, Element)]}
                          for d in ws.documents],
            "elements": elements,
            "statements": statements,
            "relations": self.list_relationships()["relations"],
            "diagnostics": [d.to_json() for d in ws.diagnostics()],
            "validation": self._counts(),
            "can_undo": bool(self._undo),
            "can_redo": bool(self._redo),
            "unsaved_changes": self._unsaved,
            "version": self.version,
        }

    def list_kinds(self) -> dict:
        """Reference card: element kinds, relationship kinds and value rules."""
        return {
            "element_kinds": supported_kinds(),
            "relationship_kinds": {
                "connect": "connect <source> to <target>: links two usages (usually ports)",
                "satisfy": "satisfy <target requirement> by <source element>",
                "allocate": "allocate <source> to <target>: e.g. function to component",
            },
            "typing_rules": {k: sorted(v) for k, v in COMPATIBLE_TYPING.items()},
            "builtin_types": [self.ws.id_of(e) for e in self.ws.elements(include_library=True)
                              if e.library and e.is_definition],
            "directions": list(DIRECTIONS),
            "value_rules": "numbers/booleans as JSON values; strings like '60 [L]' or '3.5' are "
                           "numbers (with unit), 'true'/'false' booleans, anything else a string "
                           "literal; pass value_kind='ref' to reference another element",
            "id_format": "qualified names joined by '.', e.g. Pkg.Vehicle.engine; quote names "
                         "with spaces: Pkg.'Fuel pump'. Feature chains through types are "
                         "accepted: Pkg.Vehicle.tank.fuelOut",
        }

    # ================================================================ writing

    def create_document(self, path, package=None) -> dict:
        """Create a new .sysml document (optionally with a root package)."""
        def run():
            if not isinstance(path, str) or not path.endswith(".sysml"):
                raise OpError("invalid_argument", "path must end with .sysml")
            if self.ws.document(path) is not None or os.path.exists(path):
                raise OpError("already_exists", f"document '{path}' already exists")
            doc = Document(path)
            self.ws.documents.append(doc)
            created = []
            if package:
                self._check_root_name(package, doc)
                pkg = Element("package", False, name=package, document=doc)
                doc.members.append(pkg)
                self.ws.reindex()
                created.append(self.ws.id_of(pkg))
            self.ws.reindex()
            return {"document": path, "created": created}
        return self._mutate(f"create document {path}", run)

    def _check_root_name(self, name, doc):
        if not isinstance(name, str) or not name.strip():
            raise OpError("invalid_name", "name must be a non-empty string")
        for d in self.ws.documents:
            for m in d.members:
                if isinstance(m, Element) and m.effective_name == name:
                    raise OpError("duplicate_name", f"top-level element {name} already exists")

    def create_element(self, kind, name, owner=None, document=None, type=None, value=None,
                       value_kind=None, doc=None, multiplicity=None, direction=None,
                       specializes=None, abstract=False, index=None) -> dict:
        """Create a package, definition or usage inside `owner` (an element id).

        kind: 'package', '<kw> def' (definition) or '<kw>' (usage), kw in part, port,
        attribute, item, action, state, requirement, constraint, interface, connection,
        allocation, enum, occurrence, calc, use case, verification, analysis, view...
        type: id of a definition (usages only), e.g. 'Pkg.Engine' or 'Real'.
        specializes: id(s) of definitions to specialize (definitions) or features to subset (usages).
        value: number, boolean or string; see list_kinds().value_rules.
        """
        def run():
            keyword, is_def = normalize_kind(kind)
            if owner:
                own = self._element(owner, "owner")
                if keyword == "package" and not own.is_package:
                    raise OpError("invalid_owner", "packages can only be owned by packages (or be top level)")
                doc_ = own.document
                self._check_name(name, own, doc_)
                members = own.members
            else:
                own = None
                doc_ = self._document_for_root(document)
                self._check_root_name(name, doc_)
                members = doc_.members
            el = Element(keyword, is_def, name=name, abstract=bool(abstract), owner=own, document=doc_)
            self._apply_properties(el, own, doc_, type=type, value=value, value_kind=value_kind,
                                   doc=doc, multiplicity=multiplicity, direction=direction,
                                   specializes=specializes)
            self._insert(members, el, index)
            self.ws.reindex()
            return {"created": self.ws.id_of(el), "source": serialize_node(el)}
        return self._mutate(f"create {kind} {name}", run)

    def _apply_properties(self, el, scope, document, type=None, value=None, value_kind=None, doc=None,
                          multiplicity=None, direction=None, specializes=None, clear=()):
        usage_only = {"type": type, "value": value, "multiplicity": multiplicity, "direction": direction}
        for key, val in usage_only.items():
            if val is not None and not el.is_usage:
                raise OpError("invalid_property", f"'{key}' only applies to usages, not to a {el.kind}")
        if specializes is not None and el.is_package:
            raise OpError("invalid_property", "packages cannot specialize anything")
        if type is not None:
            types = [type] if isinstance(type, str) else list(type)
            el.types = [self._type_ref(scope, document, t, el.keyword) for t in types]
        if specializes is not None:
            sups = [specializes] if isinstance(specializes, str) else list(specializes)
            refs = []
            for s in sups:
                if el.is_definition:
                    target = self._element(s, "supertype", allow_library=True)
                    if not target.is_definition:
                        raise OpError("not_a_definition", f"{self.ws.id_of(target)} is not a definition")
                    if target is el or any(a is el for a in [target, *target.ancestors()]):
                        raise OpError("invalid_argument", "an element cannot specialize itself or its owner")
                    refs.append(self.ws.ref_to(target, scope, document))
                else:
                    res = self._resolve_path(s, "subsetted feature")
                    refs.append(self.ws.ref_for(res.path, res.hops, scope, document) or Ref(res.target.path_names()))
            el.specializes = refs
        if value is not None:
            v = _value_from_input(value, value_kind)
            if v.kind == "ref":
                res = self._resolve_path(str(v.ref), "value reference")
                v.ref = self.ws.ref_for(res.path, res.hops, scope, document) or v.ref
            el.value = v
        if doc is not None:
            if not isinstance(doc, str):
                raise OpError("invalid_argument", "doc must be a string")
            if "*/" in doc:
                raise OpError("invalid_argument", "doc text cannot contain '*/'")
            el.doc = doc.strip()
        if multiplicity is not None:
            m = str(multiplicity).strip().strip("[]").replace(" ", "")
            if not _MULTIPLICITY.match(m):
                raise OpError("invalid_argument", f"invalid multiplicity '{multiplicity}'; use e.g. 1, 0..1, 1..*, *")
            el.multiplicity = m
        if direction is not None:
            if direction not in DIRECTIONS:
                raise OpError("invalid_argument", "direction must be 'in', 'out' or 'inout'")
            el.direction = direction
        for key in clear or ():
            if key == "type":
                el.types = []
            elif key == "value":
                el.value = None
            elif key == "doc":
                el.doc = None
            elif key == "multiplicity":
                el.multiplicity = None
            elif key == "direction":
                el.direction = None
            elif key == "specializes":
                el.specializes = []
            else:
                raise OpError("invalid_argument", f"cannot clear '{key}'",
                              suggestions=["type", "value", "doc", "multiplicity", "direction", "specializes"])

    def update_element(self, element, name=None, type=None, value=None, value_kind=None, doc=None,
                       multiplicity=None, direction=None, specializes=None, abstract=None, clear=None) -> dict:
        """Change properties of an element. Omitted arguments are left unchanged;
        list property names in `clear` to remove them. Renaming rewrites every reference."""
        def run():
            el = self._element(element)
            scope, document = el.owner, el.document
            captured = self._capture_refs()
            if clear:
                bad = [c for c in clear if c in ("type", "value", "multiplicity", "direction") and not el.is_usage]
                if bad:
                    raise OpError("invalid_property", f"{bad} only apply to usages")
            self._apply_properties(el, scope, document, type=type, value=value, value_kind=value_kind,
                                   doc=doc, multiplicity=multiplicity, direction=direction,
                                   specializes=specializes, clear=clear or ())
            if abstract is not None:
                el.abstract = bool(abstract)
            old_id = self.ws.id_of(el)
            if name is not None and name != el.effective_name:
                if el.owner is None:
                    self._check_root_name(name, document)
                else:
                    self._check_name(name, el.owner, document, exclude=el)
                el.name = name
            rewritten, broken = self._reanchor(captured)
            if broken:
                raise OpError("would_break_references",
                              "the change would leave references that can no longer be written", details=broken)
            out = {"updated": self.ws.id_of(el), "source": serialize_node(el)}
            if self.ws.id_of(el) != old_id:
                out["previous_id"] = old_id
            if rewritten:
                out["rewritten_references"] = rewritten
            return out
        return self._mutate(f"update {element}", run)

    def _dependents(self, el):
        subtree = {id(n) for n in el.walk()}
        deps = []
        for node, role, ref in self.ws.all_refs():
            if id(node) in subtree:
                continue
            res = self.ws.resolve_node_ref(node, ref)
            if res.ok and any(id(p) in subtree for p in res.path):
                deps.append((node, role, ref))
        return deps

    def delete_element(self, element, cascade=False) -> dict:
        """Delete an element and everything it owns. Refuses if other elements reference it,
        unless cascade=True, which also removes those references (typings, statements...)."""
        def run():
            el = self._element(element)
            eid = self.ws.id_of(el)
            deps = self._dependents(el)
            if deps and not cascade:
                raise OpError("has_dependents",
                              f"{eid} is referenced {len(deps)} time(s); delete with cascade=true "
                              f"to also remove these references",
                              details=[{"in": self._label(n), "role": r, "ref": str(ref)} for n, r, ref in deps])
            removed_refs = []
            for node, role, ref in deps:
                removed_refs.append({"in": self._label(node), "role": role, "ref": str(ref)})
                if isinstance(node, (Relationship, Import)):
                    container = node.owner.members if node.owner else node.document.members
                    if node in container:
                        container.remove(node)
                elif role == "type":
                    node.types.remove(ref)
                elif role == "specializes":
                    node.specializes.remove(ref)
                elif role == "redefines":
                    if node.name is None:
                        node.name = node.effective_name
                    node.redefines.remove(ref)
                elif role == "value":
                    node.value = None
            container = el.owner.members if el.owner else el.document.members
            container.remove(el)
            self.ws.reindex()
            out = {"deleted": eid}
            if removed_refs:
                out["removed_references"] = removed_refs
            return out
        return self._mutate(f"delete {element}", run)

    def move_element(self, element, new_owner=None, document=None, index=None) -> dict:
        """Move an element under another owner (or to the top level of `document`).
        References to and from the moved element are rewritten."""
        def run():
            el = self._element(element)
            old_id = self.ws.id_of(el)
            if new_owner:
                dest = self._element(new_owner, "new_owner")
                if dest is el or any(a is el for a in dest.ancestors()):
                    raise OpError("invalid_owner", "cannot move an element into itself or its own members")
                if el.is_package and not dest.is_package:
                    raise OpError("invalid_owner", "packages can only be owned by packages")
                self._check_name(el.effective_name, dest, dest.document, exclude=el)
                dest_doc, dest_members = dest.document, dest.members
            else:
                dest = None
                dest_doc = self._document_for_root(document)
                for m in dest_doc.members:
                    if isinstance(m, Element) and m is not el and m.effective_name == el.effective_name:
                        raise OpError("duplicate_name", f"top-level {el.effective_name} already exists")
                dest_members = dest_doc.members
            captured = self._capture_refs()
            container = el.owner.members if el.owner else el.document.members
            container.remove(el)
            el.owner = dest
            self._set_document(el, dest_doc)
            self._insert(dest_members, el, index)
            rewritten, broken = self._reanchor(captured)
            if broken:
                raise OpError("would_break_references", "moving would leave unwritable references",
                              details=broken)
            out = {"moved": self.ws.id_of(el), "previous_id": old_id}
            if rewritten:
                out["rewritten_references"] = rewritten
            return out
        return self._mutate(f"move {element}", run)

    def _relationship_owner(self, sp, tp):
        ext_s = list(reversed(list(sp[0].ancestors()))) + sp
        ext_t = list(reversed(list(tp[0].ancestors()))) + tp
        owner = None
        for a, b in zip(ext_s[:-1], ext_t[:-1]):
            if a is not b:
                break
            owner = a
        return owner

    def add_relationship(self, kind, source, target, owner=None) -> dict:
        """Add `connect source to target`, `satisfy target by source` or `allocate source to target`.

        source/target are element ids or feature chains (Pkg.Vehicle.tank.fuelOut).
        For satisfy, target is the requirement and source the satisfying element.
        The statement is placed in `owner`, by default the closest common owner."""
        def run():
            if kind not in RELATIONSHIP_KINDS:
                raise OpError("invalid_kind", f"unknown relationship kind '{kind}'", suggestions=list(RELATIONSHIP_KINDS))
            s = self._resolve_path(source, "source")
            t = self._resolve_path(target, "target")
            if kind == "connect":
                for res, label in ((s, "source"), (t, "target")):
                    if not res.target.is_usage:
                        raise OpError("invalid_endpoint",
                                      f"connect {label} {self.ws.id_of(res.target)} is a {res.target.kind}; "
                                      f"connect usages (e.g. Pkg.Vehicle.tank.fuelOut)")
                if s.target is t.target and len(s.path) == len(t.path):
                    raise OpError("invalid_endpoint", "cannot connect an element to itself")
            if kind == "satisfy" and t.target.keyword != "requirement":
                raise OpError("invalid_endpoint", f"satisfy target {self.ws.id_of(t.target)} is a "
                                                  f"{t.target.kind}, not a requirement")
            if owner:
                own = self._element(owner, "owner")
            else:
                own = self._relationship_owner(s.path, t.path)
            if own is None:
                raise OpError("owner_required", "source and target share no common owner; pass 'owner'")
            if own.library:
                raise OpError("read_only", "cannot add statements to library elements")
            sref = self.ws.ref_for(s.path, s.hops, own, own.document)
            tref = self.ws.ref_for(t.path, t.hops, own, own.document)
            if sref is None or tref is None:
                raise OpError("not_reachable",
                              f"cannot write a reference to the endpoints from {self.ws.id_of(own)}; "
                              f"choose another owner")
            for existing in self._find_statements(kind, s, t):
                raise OpError("already_exists", f"relationship already exists: {self._label(existing)}")
            rel = Relationship(kind, sref, tref, owner=own, document=own.document)
            self._insert(own.members, rel)
            self.ws.reindex()
            return {"created": self._statement_json(rel)}
        return self._mutate(f"add {kind}", run)

    def _find_statements(self, kind, s, t, loose=False):
        def matches(stmt_res, given):
            if not stmt_res.ok:
                return False
            ext = list(reversed(list(given.path[0].ancestors()))) + given.path
            sp = stmt_res.path
            if loose:
                return sp[-1] is given.path[-1]
            return len(sp) <= len(ext) and all(a is b for a, b in zip(sp, ext[len(ext) - len(sp):]))
        out = []
        for node in self.ws.nodes():
            if isinstance(node, Relationship) and node.kind == kind:
                if matches(self.ws.resolve_node_ref(node, node.source), s) and \
                        matches(self.ws.resolve_node_ref(node, node.target), t):
                    out.append(node)
        return out

    def remove_relationship(self, kind=None, source=None, target=None, statement=None, owner=None) -> dict:
        """Remove a connect/satisfy/allocate statement, identified either by `kind` + its
        endpoints, or by its exact `statement` text (e.g. 'satisfy R by x;', as shown by
        validate/get_element) - the only way to remove a statement whose references are broken."""
        def run():
            if statement is not None:
                text = " ".join(str(statement).split())
                if not text.endswith(";"):
                    text += ";"
                own = self._element(owner, "owner") if owner else None
                found = [n for n in self.ws.nodes() if isinstance(n, Relationship)
                         and render_statement(n) == text and (own is None or n.owner is own)]
                if not found:
                    near = [render_statement(n) for n in self.ws.nodes() if isinstance(n, Relationship)]
                    raise OpError("not_found", f"no statement '{text}'",
                                  suggestions=difflib.get_close_matches(text, near, n=3, cutoff=0.5))
            else:
                if kind not in RELATIONSHIP_KINDS:
                    raise OpError("invalid_kind", f"unknown relationship kind '{kind}'",
                                  suggestions=list(RELATIONSHIP_KINDS))
                if not source or not target:
                    raise OpError("invalid_argument", "give kind + source + target, or statement")
                s = self._resolve_path(source, "source")
                t = self._resolve_path(target, "target")
                found = self._find_statements(kind, s, t) or self._find_statements(kind, s, t, loose=True)
                if not found:
                    raise OpError("not_found", f"no {kind} between {source} and {target}")
            if len(found) > 1:
                raise OpError("ambiguous", "several statements match; use full feature chains or pass owner",
                              details=[self._statement_json(f) for f in found])
            node = found[0]
            info = self._statement_json(node)
            container = node.owner.members if node.owner else node.document.members
            container.remove(node)
            self.ws.reindex()
            return {"removed": info}
        return self._mutate(f"remove {kind or 'statement'}", run)

    def add_import(self, namespace, path, visibility=None) -> dict:
        """Add `import <path>;` to a namespace. path like 'ISQ::*' or 'Pkg::Element'."""
        def run():
            own = self._element(namespace, "namespace")
            if visibility not in (None, "private", "public"):
                raise OpError("invalid_argument", "visibility must be 'private', 'public' or omitted")
            parts = [p.strip() for p in str(path).split("::")]
            wildcard = parts.pop() if parts and parts[-1] in ("*", "**") else None
            if any(p in ("*", "**", "") for p in parts):
                raise OpError("invalid_argument", "wildcards are only allowed as the last segment")
            names = [parse_ref(p).segments[0] for p in parts]
            imp = Import(names, wildcard=wildcard, visibility=visibility, owner=own, document=own.document)
            res = self.ws._resolve_uncached(imp.path_ref(), None, None, allow_heuristic=False) if names else None
            if names and not res.ok:
                raise OpError("not_found", f"cannot resolve import path '{path}' from the root",
                              suggestions=self.ws.suggest(names[-1]))
            for m in own.members:
                if isinstance(m, Import) and m.path == imp.path and m.wildcard == imp.wildcard:
                    raise OpError("already_exists", "this import already exists")
            self._insert(own.members, imp)
            self.ws.reindex()
            return {"created": render_statement(imp), "namespace": self.ws.id_of(own)}
        return self._mutate(f"import {path}", run)

    def remove_import(self, namespace, path) -> dict:
        """Remove `import <path>;` from a namespace."""
        def run():
            own = self._element(namespace, "namespace")
            for m in own.members:
                if isinstance(m, Import) and m.render_path() == str(path).replace(" ", ""):
                    own.members.remove(m)
                    self.ws.reindex()
                    return {"removed": render_statement(m)}
            raise OpError("not_found", f"no import '{path}' in {namespace}",
                          suggestions=[m.render_path() for m in own.members if isinstance(m, Import)])
        return self._mutate(f"remove import {path}", run)

    def add_from_text(self, text, owner=None, document=None) -> dict:
        """Parse a SysML v2 snippet and add its members to `owner` (or top level of `document`).
        Fails without changing anything if the snippet has syntax errors or introduces
        new validation errors."""
        def run():
            try:
                snippet = parse_document(text, "<snippet>")
            except SysmlSyntaxError as exc:
                raise OpError("syntax_error", str(exc), details=exc.to_json()) from exc
            before = {(d.code, d.message) for d in self.ws.diagnostics() if d.severity == "error"}
            if owner:
                own = self._element(owner, "owner")
                doc_, members = own.document, own.members
            else:
                own, doc_ = None, self._document_for_root(document)
                members = doc_.members
            created = []
            for node in snippet.members:
                if isinstance(node, Element):
                    if own is not None:
                        self._check_name(node.effective_name, own, doc_)
                    else:
                        self._check_root_name(node.effective_name, doc_)
                    if node.is_package and own is not None and not own.is_package:
                        raise OpError("invalid_owner", "packages can only be owned by packages")
                node.owner = own
                self._set_document(node, doc_)
                self._insert(members, node)
                self.ws.reindex()
                if isinstance(node, Element):
                    created.append(self.ws.id_of(node))
            after = [d for d in self.ws.diagnostics() if d.severity == "error"
                     and (d.code, d.message) not in before]
            if after:
                raise OpError("introduces_errors", "the snippet introduces validation errors",
                              details=[d.to_json() for d in after])
            return {"created": created}
        return self._mutate("add from text", run)

    def apply_batch(self, operations) -> dict:
        """Apply several mutating operations atomically: [{"op": "create_element", "args": {...}}, ...].
        If any fails, nothing is applied and the failing index is reported."""
        if not isinstance(operations, list) or not operations:
            raise OpError("invalid_argument", "operations must be a non-empty list")

        def run():
            results = []
            for i, item in enumerate(operations):
                if not isinstance(item, dict) or item.get("op") not in MUTATING_OPERATIONS:
                    raise OpError("invalid_argument", f"operation {i}: 'op' must be one of {list(MUTATING_OPERATIONS)}")
                args = item.get("args") or {}
                if not isinstance(args, dict):
                    raise OpError("invalid_argument", f"operation {i}: 'args' must be an object")
                try:
                    res = getattr(self, item["op"])(**args)
                except OpError as exc:
                    raise OpError("batch_failed", f"operation {i} ({item['op']}) failed: {exc.message}",
                                  suggestions=exc.suggestions,
                                  details={"index": i, "error": exc.to_json()}) from exc
                except TypeError as exc:
                    raise OpError("batch_failed", f"operation {i} ({item['op']}): {exc}",
                                  details={"index": i}) from exc
                res.pop("validation", None)
                results.append(res)
            return {"results": results}
        return self._mutate("batch", run)

    # ============================================================ history/io

    def undo(self) -> dict:
        """Undo the last change."""
        if not self._undo:
            raise OpError("nothing_to_undo", "no change to undo")
        entry = self._undo.pop()
        self._redo.append({"label": entry["label"], "snap": self._snapshot()})
        self._restore(entry["snap"])
        self.version += 1
        self._unsaved = True
        out = {"undone": entry["label"], "validation": self._counts()}
        if self.autosave:
            out["saved"] = self._save()
        return out

    def redo(self) -> dict:
        """Redo the last undone change."""
        if not self._redo:
            raise OpError("nothing_to_redo", "no change to redo")
        entry = self._redo.pop()
        self._undo.append({"label": entry["label"], "snap": self._snapshot()})
        self._restore(entry["snap"])
        self.version += 1
        self._unsaved = True
        out = {"redone": entry["label"], "validation": self._counts()}
        if self.autosave:
            out["saved"] = self._save()
        return out

    def _save(self):
        written = []
        for d in self.ws.documents:
            text = serialize_document(d)
            if self._disk.get(d.path) != text:
                os.makedirs(os.path.dirname(os.path.abspath(d.path)), exist_ok=True)
                with open(d.path, "w", encoding="utf-8") as f:
                    f.write(text)
                self._disk[d.path] = text
                written.append(d.path)
        self._unsaved = False
        self._stamps = self._file_stamps()
        return written

    def save(self) -> dict:
        """Write changed documents to disk (canonical SysML v2 text)."""
        return {"written": self._save()}

    def diff(self) -> dict:
        """Unified diff between the files on disk and the in-memory model."""
        out = []
        for d in self.ws.documents:
            new = serialize_document(d)
            old = self._disk.get(d.path) or ""
            if new != old:
                out.append("".join(difflib.unified_diff(
                    old.splitlines(True), new.splitlines(True), fromfile=f"a/{d.path}", tofile=f"b/{d.path}")))
        return {"diff": "\n".join(out), "changed_documents": len(out)}

    def reload(self) -> dict:
        """Discard in-memory changes and reload documents from disk."""
        paths = [d.path for d in self.ws.documents]
        self.ws = Workspace.load(*paths) if paths else Workspace()
        self._disk = {d.path: _read(d.path) for d in self.ws.documents}
        self._undo.clear()
        self._redo.clear()
        self._unsaved = False
        self._stamps = self._file_stamps()
        self.version += 1
        return {"reloaded": paths, "validation": self._counts()}


def _normalize_words(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[^\w]+|_", " ", text)
    return " ".join(text.lower().split())


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# Public operation catalogue (name -> method name), shared by the MCP
# server, the CLI and the web API.
READ_OPERATIONS = (
    "summary", "list_elements", "get_element", "get_tree", "find_elements",
    "list_relationships", "impact_analysis", "requirements_trace", "validate",
    "get_source", "list_kinds", "diff", "export_model",
)
CONTROL_OPERATIONS = ("apply_batch", "undo", "redo", "save", "reload")
ALL_OPERATIONS = READ_OPERATIONS + MUTATING_OPERATIONS + CONTROL_OPERATIONS


def call(service: ModelService, op: str, args: dict | None = None) -> dict:
    """Invoke an operation by name, always returning a JSON-able dict."""
    if op not in ALL_OPERATIONS:
        close = difflib.get_close_matches(op, ALL_OPERATIONS, n=3)
        return {"ok": False, "error": {"code": "unknown_operation", "message": f"unknown operation '{op}'",
                                       "suggestions": close}}
    try:
        if op != "reload":
            service.sync(for_write=op in MUTATING_OPERATIONS or op in ("apply_batch", "undo", "redo", "save"))
        result = getattr(service, op)(**(args or {}))
    except OpError as exc:
        return {"ok": False, "error": exc.to_json()}
    except TypeError as exc:
        return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
    result.setdefault("ok", True)
    return result
