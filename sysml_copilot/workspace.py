"""A set of SysML documents loaded together: indexing, name resolution,
derived relations and validation.

Element IDs are qualified names: the dot path from the root namespace
(`VehicleFuelSystem.Vehicle.pump`), with non-identifier segments quoted
(`Pkg.'Fuel pump'`). Feature chains through a usage's type are also
accepted anywhere an ID is expected (`VehicleFuelSystem.Vehicle.tank.fuelOut`
means "the `fuelOut` port of the `tank` part", even though `fuelOut` is
declared on `FuelTank`).

Resolution rules (a pragmatic subset of SysML v2 scoping):

1. The first segment of a reference is looked up in the reference's
   namespace, then each enclosing namespace outward, then the root
   namespace (all top-level elements of every document), then the
   built-in library (implicitly imported).
2. Looking a name up *in* a namespace checks, in order: its owned members,
   members brought in by its `import`s, then members inherited through its
   types (`:`) and supertypes (`:>`). Inheritance through typing is what
   lets `connect tank.fuelOut to pump.fuelIn` resolve.
3. Each further segment is looked up *in* the element the previous segment
   resolved to, with rule 2.
4. Legacy fallback: if rules 1-3 find nothing for the first segment, a
   unique element with that name anywhere in the model is accepted, but
   reported as a `heuristic_reference` warning.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field

from .model import (
    COMPATIBLE_TYPING,
    Alias,
    Document,
    Element,
    Import,
    Ref,
    Relationship,
    format_path,
    separators_for,
)
from .syntax import parse_document, parse_ref, serialize_document, SysmlSyntaxError

LIBRARY_PATH = "<library>/ScalarValues.sysml"
LIBRARY_TEXT = """
package ScalarValues {
    abstract attribute def ScalarValue;
    attribute def Boolean :> ScalarValue;
    attribute def String :> ScalarValue;
    abstract attribute def NumericalValue :> ScalarValue;
    abstract attribute def Number :> NumericalValue;
    attribute def Complex :> Number;
    attribute def Real :> Complex;
    attribute def Rational :> Real;
    attribute def Integer :> Rational;
    attribute def Natural :> Integer;
    attribute def Positive :> Natural;
}
"""

# Hop kinds recorded on resolution paths.
HOP_SCOPE = "scope"  # first segment, found by scope lookup
HOP_MEMBER = "member"  # owned member of the previous element
HOP_IMPORT = "import"  # brought in by an import
HOP_INHERITED = "inherited"  # found on a type / supertype of the previous element
HOP_HEURISTIC = "heuristic"  # first segment found only by global name search


@dataclass
class Resolution:
    path: list[Element] | None
    hops: list[str] = field(default_factory=list)
    error: str | None = None  # None | "unresolved" | "ambiguous"
    failed_segment: int | None = None
    candidates: list[Element] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.path is not None

    @property
    def target(self) -> Element | None:
        return self.path[-1] if self.path else None

    @property
    def heuristic(self) -> bool:
        return bool(self.hops) and self.hops[0] == HOP_HEURISTIC


@dataclass
class Edge:
    type: str
    source: Element
    target: Element
    origin: object  # the node declaring it
    source_path: list[Element] | None = None
    target_path: list[Element] | None = None


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    element: str | None = None
    document: str | None = None
    suggestions: list[str] = field(default_factory=list)

    def to_json(self):
        data = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.element:
            data["element"] = self.element
        if self.document:
            data["document"] = self.document
        if self.suggestions:
            data["suggestions"] = self.suggestions
        return data


def reference_scope(node):
    """(namespace, document) in which `node`'s own references resolve."""
    if isinstance(node, Element):
        return node.owner, node.document
    return node.owner, node.document


class Workspace:
    def __init__(self):
        self.documents: list[Document] = []
        self.library = parse_document(LIBRARY_TEXT, LIBRARY_PATH)
        self.library.library = True
        for node in self.library.walk():
            if isinstance(node, Element):
                node.library = True
        self.reindex()

    # ------------------------------------------------------------------ io

    @classmethod
    def from_text(cls, text: str, path: str = "model.sysml") -> "Workspace":
        ws = cls()
        ws.documents.append(parse_document(text, path))
        ws.reindex()
        return ws

    @classmethod
    def load(cls, *paths: str) -> "Workspace":
        ws = cls()
        for p in paths:
            for file in _expand(p):
                with open(file, encoding="utf-8") as f:
                    ws.documents.append(parse_document(f.read(), file))
        ws.reindex()
        return ws

    def document(self, path: str) -> Document | None:
        for d in self.documents:
            if d.path == path or os.path.abspath(d.path) == os.path.abspath(path):
                return d
        return None

    def texts(self) -> dict[str, str]:
        return {d.path: serialize_document(d) for d in self.documents}

    # --------------------------------------------------------------- index

    def all_documents(self):
        return [*self.documents, self.library]

    def elements(self, include_library=False):
        docs = self.all_documents() if include_library else self.documents
        for d in docs:
            for n in d.walk():
                if isinstance(n, Element):
                    yield n

    def nodes(self):
        for d in self.documents:
            yield from d.walk()

    def reindex(self):
        self.by_id: dict[str, Element] = {}
        self._id_of: dict[int, str] = {}
        self.duplicates: dict[str, list[Element]] = {}
        self._cache: dict[int, Resolution] = {}
        self._in_progress: set[int] = set()
        self._edges = None
        self._diagnostics = None
        for d in self.all_documents():
            for n in d.walk():
                if isinstance(n, Element):
                    self._index(n)

    def _index(self, el: Element):
        if el.effective_name is None:
            owner_id = self._id_of.get(id(el.owner)) if el.owner else None
            siblings = el.owner.members if el.owner else el.document.members
            local = f"<{el.kind}#{siblings.index(el)}>"
            eid = f"{owner_id}.{local}" if owner_id else local
        else:
            eid = format_path(el.path_names())
        if eid in self.by_id:
            self.duplicates.setdefault(eid, [self.by_id[eid]]).append(el)
        else:
            self.by_id[eid] = el
        self._id_of[id(el)] = eid

    def id_of(self, el: Element) -> str:
        return self._id_of[id(el)]

    def get(self, element_id: str) -> Element | None:
        """Exact ID lookup (no feature chains)."""
        return self.by_id.get(element_id)

    def lookup(self, text: str) -> Resolution:
        """Resolve an ID or feature chain from the root namespace."""
        el = self.by_id.get(text)
        if el is not None:
            chain = list(reversed(list(el.ancestors()))) + [el]
            return Resolution(chain, [HOP_SCOPE] + [HOP_MEMBER] * (len(chain) - 1))
        try:
            ref = parse_ref(text)
        except SysmlSyntaxError:
            return Resolution(None, error="unresolved", failed_segment=0)
        return self._resolve_uncached(ref, None, None, allow_heuristic=False)

    # ---------------------------------------------------------- resolution

    def resolve_node_ref(self, node, ref: Ref) -> Resolution:
        """Resolve a reference held by `node` (cached until next reindex)."""
        key = id(ref)
        if key in self._cache:
            return self._cache[key]
        if key in self._in_progress:
            return Resolution(None, error="unresolved", failed_segment=0)
        self._in_progress.add(key)
        try:
            if isinstance(node, Import):
                res = self._resolve_import_target(node)
            elif isinstance(node, Element) and node.owner is not None and any(r is ref for r in node.redefines):
                res = self._resolve_redefined(node, ref)
            else:
                scope, document = reference_scope(node)
                res = self._resolve_uncached(ref, scope, document)
        finally:
            self._in_progress.discard(key)
        self._cache[key] = res
        return res

    def _resolve_redefined(self, node: Element, ref: Ref) -> Resolution:
        """A redefined feature is looked up among what the owner *inherits*
        (its types and supertypes), never among its own members - otherwise
        `attribute :>> mass` would find itself."""
        owner = node.owner
        if len(ref.segments) == 1:
            visited = {(id(owner), ref.segments[0])}
            for sref in [*owner.types, *owner.specializes, *owner.redefines]:
                sup = self.resolve_node_ref(owner, sref)
                if sup.ok:
                    hits, _ = self._lookup_in(sup.target, ref.segments[0], visited)
                    if hits:
                        return Resolution([hits[0]], [HOP_INHERITED])
        scope, document = reference_scope(node)
        return self._resolve_uncached(ref, scope, document)

    def resolve(self, ref: Ref, scope: Element | None, document: Document | None,
                allow_heuristic=True) -> Resolution:
        return self._resolve_uncached(ref, scope, document, allow_heuristic)

    def _resolve_uncached(self, ref, scope, document, allow_heuristic=True) -> Resolution:
        segs = ref.segments
        found, hop = self._resolve_first(segs[0], scope, document)
        if not found and allow_heuristic:
            matches = [e for e in self.elements() if e.effective_name == segs[0]]
            if len(matches) == 1:
                found, hop = matches, HOP_HEURISTIC
            elif len(matches) > 1:
                return Resolution(None, error="ambiguous", failed_segment=0, candidates=matches)
        if not found:
            return Resolution(None, error="unresolved", failed_segment=0)
        path, hops = [found[0]], [hop]
        for i, seg in enumerate(segs[1:], start=1):
            nxt, how = self._lookup_in(path[-1], seg, set())
            if not nxt:
                return Resolution(None, error="unresolved", failed_segment=i)
            path.append(nxt[0])
            hops.append(how)
        return Resolution(path, hops)

    def _resolve_first(self, name, scope, document):
        ns = scope
        while ns is not None:
            found, how = self._lookup_in(ns, name, set())
            if found:
                return found, HOP_SCOPE if how == HOP_MEMBER else how
            ns = ns.owner
        roots = [m for d in self.documents for m in d.members
                 if isinstance(m, Element) and m.effective_name == name]
        if roots:
            return roots, HOP_SCOPE
        for d in self.documents:
            aliased = self._aliased(d.members, name)
            if aliased:
                return aliased, HOP_IMPORT
        if document is not None:
            imported = self._imported(document.members, name, set())
            if imported:
                return imported, HOP_IMPORT
        lib_roots = [m for m in self.library.members if isinstance(m, Element)]
        for root in lib_roots:
            if root.effective_name == name:
                return [root], HOP_SCOPE
        for root in lib_roots:
            hits = [m for m in root.element_members() if m.effective_name == name]
            if hits:
                return hits, HOP_IMPORT
        return [], None

    def _lookup_in(self, ns: Element, name: str, visited: set):
        """Find `name` visible inside namespace `ns` (owned, imported, inherited)."""
        key = (id(ns), name)
        if key in visited:
            return [], None
        visited.add(key)
        owned = [m for m in ns.members if isinstance(m, Element) and m.effective_name == name]
        if owned:
            return owned, HOP_MEMBER
        aliased = self._aliased(ns.members, name)
        if aliased:
            return aliased, HOP_IMPORT
        imported = self._imported(ns.members, name, visited)
        if imported:
            return imported, HOP_IMPORT
        for _, ref in [("type", r) for r in ns.types] + [("sup", r) for r in ns.specializes] + \
                [("redef", r) for r in ns.redefines]:
            res = self.resolve_node_ref(ns, ref)
            if res.ok:
                hits, _ = self._lookup_in(res.target, name, visited)
                if hits:
                    return hits, HOP_INHERITED
        return [], None

    def _aliased(self, members, name):
        for m in members:
            if isinstance(m, Alias) and m.name == name:
                res = self.resolve_node_ref(m, m.target)
                if res.ok:
                    return [res.target]
        return []

    def _imported(self, members, name, visited):
        for imp in members:
            if not isinstance(imp, Import):
                continue
            res = self.resolve_node_ref(imp, imp.path_ref())
            if not res.ok:
                continue
            target = res.target
            if imp.wildcard is None:
                if target.effective_name == name:
                    return [target]
            elif imp.wildcard == "*":
                hits, _ = self._lookup_in(target, name, visited)
                if hits:
                    return hits
            else:  # "**"
                hits = [e for e in target.walk() if isinstance(e, Element)
                        and e is not target and e.effective_name == name]
                if hits:
                    return hits
        return []

    def _resolve_import_target(self, imp: Import) -> Resolution:
        if not imp.path:
            return Resolution(None, error="unresolved", failed_segment=0)
        # Import paths are resolved from the root (then the library), not
        # through the importing namespace's own imports.
        return self._resolve_uncached(imp.path_ref(), None, None, allow_heuristic=False)

    # --------------------------------------------------------- ref writing

    def ref_for(self, path: list[Element], hops: list[str], scope, document) -> Ref | None:
        """Shortest reference from `scope` that resolves to the same `path`.

        A prefix of the path may only be collapsed if every hop inside it
        is plain ownership; otherwise the chain (e.g. `tank.fuelOut`, where
        `fuelOut` is reached through `tank`'s type) must be kept.
        """
        best = None
        n = len(path)
        for k in range(n):
            if any(h not in (HOP_MEMBER, HOP_SCOPE, HOP_HEURISTIC) for h in hops[1:k + 1]):
                break
            tail = [e.effective_name for e in path[k + 1:]]
            for anchor in self._anchor_candidates(path[k], scope, document):
                cand = Ref(anchor + tail)
                res = self.resolve(cand, scope, document, allow_heuristic=False)
                if res.ok and len(res.path) >= n - k and all(
                        a is b for a, b in zip(res.path[len(res.path) - (n - k):], path[k:])):
                    cand.seps = separators_for(res.path) or None
                    if best is None or len(cand.segments) < len(best.segments):
                        best = cand
                    break
        return best

    def _anchor_candidates(self, el: Element, scope, document):
        names = el.path_names()
        for i in range(len(names) - 1, -1, -1):
            yield names[i:]

    def ref_to(self, el: Element, scope, document) -> Ref:
        chain = list(reversed(list(el.ancestors()))) + [el]
        hops = [HOP_SCOPE] + [HOP_MEMBER] * (len(chain) - 1)
        return self.ref_for(chain, hops, scope, document) or \
            Ref(el.path_names(), seps=separators_for(chain) or None)

    # -------------------------------------------------------------- derive

    def all_refs(self):
        """(node, role, ref) for every reference in user documents."""
        for node in self.nodes():
            for role, ref in node.refs():
                yield node, role, ref

    def edges(self) -> list[Edge]:
        if self._edges is None:
            self._edges = self._compute_edges()
        return self._edges

    def _compute_edges(self):
        edges = []
        for node in self.nodes():
            if isinstance(node, Element):
                if node.owner is not None:
                    edges.append(Edge("CONTAINS", node.owner, node, node))
                for ref in node.types:
                    res = self.resolve_node_ref(node, ref)
                    if res.ok:
                        edges.append(Edge("TYPED_BY", node, res.target, node))
                for ref in node.specializes:
                    res = self.resolve_node_ref(node, ref)
                    if res.ok:
                        edges.append(Edge("SPECIALIZES" if node.is_definition else "SUBSETS",
                                          node, res.target, node))
                for ref in node.redefines:
                    res = self.resolve_node_ref(node, ref)
                    if res.ok:
                        edges.append(Edge("REDEFINES", node, res.target, node))
            elif isinstance(node, Relationship):
                s = self.resolve_node_ref(node, node.source)
                t = self.resolve_node_ref(node, node.target)
                if s.ok and t.ok:
                    edges.append(Edge(node.relation_type, s.target, t.target, node, s.path, t.path))
        return edges

    # ------------------------------------------------------------ validate

    def diagnostics(self) -> list[Diagnostic]:
        if self._diagnostics is None:
            self._diagnostics = self._validate()
        return self._diagnostics

    def _validate(self) -> list[Diagnostic]:
        out: list[Diagnostic] = []

        for eid, els in self.duplicates.items():
            if any(not e.library for e in els):
                out.append(Diagnostic("error", "duplicate_name",
                                      f"{len(els)} elements share the qualified name {eid}",
                                      element=eid, document=els[0].document.path))

        for node, role, ref in self.all_refs():
            res = self.resolve_node_ref(node, ref)
            where = self._node_label(node)
            if not res.ok:
                if res.error == "ambiguous":
                    out.append(Diagnostic(
                        "error", "ambiguous_reference",
                        f"{where}: '{ref}' ({role}) matches several elements; qualify it",
                        element=self._node_element_id(node), document=node.document.path,
                        suggestions=[self.id_of(c) for c in res.candidates]))
                else:
                    seg = ref.segments[res.failed_segment or 0]
                    out.append(Diagnostic(
                        "error", "unresolved_reference",
                        f"{where}: cannot resolve '{ref}' ({role}); '{seg}' not found",
                        element=self._node_element_id(node), document=node.document.path,
                        suggestions=self.suggest(seg)))
                continue
            if res.heuristic:
                scope, document = reference_scope(node)
                better = self.ref_for(res.path, res.hops, scope, document)
                out.append(Diagnostic(
                    "warning", "heuristic_reference",
                    f"{where}: '{ref}' ({role}) is not visible from here and was matched by name "
                    f"only" + (f"; write '{better.render()}'" if better else ""),
                    element=self._node_element_id(node), document=node.document.path,
                    suggestions=[better.render()] if better else []))
            target = res.target
            if isinstance(node, Element) and role == "type":
                if not target.is_definition:
                    out.append(Diagnostic(
                        "error", "typed_by_non_definition",
                        f"{where} is typed by {self.id_of(target)}, which is a {target.kind}, "
                        f"not a definition",
                        element=self.id_of(node), document=node.document.path))
                elif node.keyword and target.keyword not in COMPATIBLE_TYPING.get(node.keyword, {node.keyword}):
                    out.append(Diagnostic(
                        "warning", "typing_kind_mismatch",
                        f"{where} ({node.keyword}) is typed by {self.id_of(target)} ({target.kind})",
                        element=self.id_of(node), document=node.document.path))
            if isinstance(node, Element) and role == "specializes" and node.is_definition:
                if not target.is_definition:
                    out.append(Diagnostic(
                        "error", "specializes_non_definition",
                        f"{where} specializes {self.id_of(target)}, which is not a definition",
                        element=self.id_of(node), document=node.document.path))
                elif self._specialization_cycle(node):
                    out.append(Diagnostic(
                        "error", "specialization_cycle",
                        f"{where} is part of a specialization cycle",
                        element=self.id_of(node), document=node.document.path))
            if isinstance(node, Relationship):
                if node.kind == "satisfy" and role == "target" and target.keyword != "requirement":
                    out.append(Diagnostic(
                        "error", "satisfy_non_requirement",
                        f"{where}: {self.id_of(target)} is a {target.kind}, not a requirement",
                        document=node.document.path))
                if node.kind == "connect" and not target.is_usage:
                    out.append(Diagnostic(
                        "warning", "connect_non_usage",
                        f"{where}: endpoint {self.id_of(target)} is a {target.kind}; connect "
                        f"usages (parts/ports), not definitions",
                        document=node.document.path))

        satisfied = {id(e.target) for e in self.edges() if e.type == "SATISFIES"}
        for el in self.elements():
            if el.keyword == "requirement" and el.is_definition and id(el) not in satisfied:
                out.append(Diagnostic("info", "requirement_not_satisfied",
                                      f"requirement {self.id_of(el)} is not satisfied by any element",
                                      element=self.id_of(el), document=el.document.path))
            if el.is_usage and not el.types and not el.specializes and not el.redefines \
                    and el.keyword in ("port", "attribute", "item"):
                out.append(Diagnostic("info", "untyped_usage",
                                      f"{el.keyword} {self.id_of(el)} has no type",
                                      element=self.id_of(el), document=el.document.path))
        order = {"error": 0, "warning": 1, "info": 2}
        out.sort(key=lambda d: (order[d.severity], d.document or "", d.element or "", d.code))
        return out

    def _specialization_cycle(self, el: Element) -> bool:
        seen, stack = set(), [el]
        while stack:
            cur = stack.pop()
            for ref in cur.specializes:
                res = self.resolve_node_ref(cur, ref)
                if not res.ok:
                    continue
                if res.target is el:
                    return True
                if id(res.target) not in seen:
                    seen.add(id(res.target))
                    stack.append(res.target)
        return False

    def _node_element_id(self, node):
        if isinstance(node, Element):
            return self.id_of(node)
        return self.id_of(node.owner) if node.owner else None

    def _node_label(self, node) -> str:
        from .syntax import render_statement
        if isinstance(node, Element):
            return self.id_of(node)
        owner = f" in {self.id_of(node.owner)}" if node.owner else ""
        return f"'{render_statement(node)}'{owner}"

    def suggest(self, name: str, limit=3) -> list[str]:
        names = {}
        for el in self.elements(include_library=True):
            if el.effective_name:
                names.setdefault(el.effective_name, []).append(self.id_of(el))
        close = difflib.get_close_matches(name, list(names), n=limit, cutoff=0.6)
        lowered = [n for n in names if n.lower() == name.lower() and n not in close]
        out = []
        for n in lowered + close:
            out.extend(names[n])
        return out[:limit]


def _expand(path):
    if os.path.isdir(path):
        found = []
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("venv", "node_modules"))
            for f in sorted(files):
                if f.endswith(".sysml"):
                    found.append(os.path.join(root, f))
        return found
    return [path]
