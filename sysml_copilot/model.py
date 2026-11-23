"""In-memory metamodel for the supported SysML v2 subset.

The model is a tree that mirrors the textual notation: a `Document` holds
top-level members, an `Element` (package, definition or usage) owns further
members, and `Relationship`/`Import` statements live inside the namespace
that declares them. Keeping the tree close to the text is what makes
lossless(ish) write-back possible: the serializer walks the same tree.

References between elements are stored as `Ref` objects (a list of name
segments, exactly as written). They are resolved by `workspace.Workspace`,
never stored as pointers, so the tree can be edited and re-serialized
without dangling object references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Keywords
# --------------------------------------------------------------------------

# Element keywords that can be used both as `<kw> def Name` (definition)
# and `<kw> name` (usage). Order matters only for documentation.
ELEMENT_KEYWORDS = [
    "part",
    "port",
    "attribute",
    "item",
    "action",
    "state",
    "requirement",
    "constraint",
    "interface",
    "connection",
    "allocation",
    "enum",
    "occurrence",
    "calc",
    "case",
    "use case",
    "verification",
    "analysis",
    "view",
    "viewpoint",
    "concern",
    "metadata",
]

# Keywords with their own grammar rule (not the generic `<kw> name : T` shape).
SPECIAL_KEYWORDS = {"metadata"}

# Kind names, e.g. "part" -> ("PartDefinition", "PartUsage").
_KIND_STEMS = {
    "part": "Part",
    "port": "Port",
    "attribute": "Attribute",
    "item": "Item",
    "action": "Action",
    "state": "State",
    "requirement": "Requirement",
    "constraint": "Constraint",
    "interface": "Interface",
    "connection": "Connection",
    "allocation": "Allocation",
    "enum": "Enumeration",
    "occurrence": "Occurrence",
    "calc": "Calculation",
    "case": "Case",
    "use case": "UseCase",
    "verification": "VerificationCase",
    "analysis": "AnalysisCase",
    "view": "View",
    "viewpoint": "Viewpoint",
    "concern": "Concern",
    "metadata": "Metadata",
}

KIND_BY_KEYWORD = {}
KEYWORD_BY_KIND = {}
for _kw, _stem in _KIND_STEMS.items():
    KIND_BY_KEYWORD[(_kw, True)] = _stem + "Definition"
    KIND_BY_KEYWORD[(_kw, False)] = _stem + "Usage"
KIND_BY_KEYWORD[("package", False)] = "Package"
KIND_BY_KEYWORD[("", False)] = "Usage"  # keyword-less usage, e.g. `:>> mass = 1200;`
for (_kw, _is_def), _kind in KIND_BY_KEYWORD.items():
    KEYWORD_BY_KIND[_kind] = (_kw, _is_def)

# Which definition keywords a usage of a given keyword may be typed by.
# Anything outside this table is reported as a typing-kind mismatch.
COMPATIBLE_TYPING = {
    "part": {"part", "item", "occurrence"},
    "item": {"item", "part", "occurrence"},
    "attribute": {"attribute", "enum"},
    "enum": {"enum"},
    "port": {"port"},
    "action": {"action", "calc"},
    "state": {"state"},
    "requirement": {"requirement", "concern"},
    "constraint": {"constraint"},
    "interface": {"interface", "connection"},
    "connection": {"connection", "interface"},
    "allocation": {"allocation"},
    "occurrence": {"occurrence", "part", "item"},
    "calc": {"calc", "case"},  # Case :> Calculation in the standard library
    "case": {"case"},
    "use case": {"use case"},
    "verification": {"verification"},
    "analysis": {"analysis"},
    "view": {"view"},
    "viewpoint": {"viewpoint"},
    "concern": {"concern"},
    "metadata": {"metadata"},
}

RESERVED_WORDS = {
    "package", "def", "abstract", "in", "out", "inout", "import", "private",
    "public", "protected", "connect", "to", "satisfy", "by", "allocate", "doc", "true",
    "false", "specializes", "subsets", "redefines", "references", "case", "use",
    "standard", "library", "alias", "for", "comment", "about", "ref", "readonly",
    "derived", "end", "constant", "variation", "variant", "individual", "ordered", "nonunique",
    "defined", "default", "and", "or", "xor", "not",
} | {kw for kw in ELEMENT_KEYWORDS if " " not in kw}

VISIBILITIES = ("private", "protected", "public")

# Feature/definition modifiers, in the canonical order they are written.
MODIFIERS = ("abstract", "variation", "variant", "individual", "readonly", "derived", "end", "constant", "ref")

DIRECTIONS = ("in", "out", "inout")

RELATIONSHIP_KINDS = ("connect", "satisfy", "allocate")

# Relationship statement kind -> graph relation type.
RELATION_TYPE_BY_STATEMENT = {
    "connect": "CONNECTS_TO",
    "satisfy": "SATISFIES",
    "allocate": "ALLOCATES",
    "flow": "FLOWS_TO",
    "dependency": "DEPENDS_ON",
}

_SIMPLE_NAME = re.compile(r"^[^\W\d]\w*$")


def is_simple_name(name: str) -> bool:
    return bool(_SIMPLE_NAME.match(name)) and name not in RESERVED_WORDS


def quote_name(name: str) -> str:
    """Render a name for the textual notation, quoting it when needed."""
    if is_simple_name(name):
        return name
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def format_path(segments) -> str:
    """Render a list of name segments as a dotted element ID / reference."""
    return ".".join(quote_name(s) for s in segments)


# --------------------------------------------------------------------------
# Tree nodes
# --------------------------------------------------------------------------


@dataclass(eq=False)
class Ref:
    """A reference as written in the text: a list of name segments."""

    segments: list[str]
    conjugated: bool = False  # `~PortDef` (typing by the conjugate of a port definition)
    # Separator written before each segment after the first: "::" qualifies a
    # name inside a namespace (Pkg::Engine), "." chains features (tank.fuelOut).
    seps: list[str] | None = None

    def __str__(self) -> str:
        """Dotted form, as used in element IDs (not necessarily valid SysML)."""
        return format_path(self.segments)

    def render(self) -> str:
        """SysML text of the reference, with its `::` / `.` separators."""
        seps = self.seps if self.seps and len(self.seps) == len(self.segments) - 1 else None
        out = quote_name(self.segments[0])
        for i, seg in enumerate(self.segments[1:]):
            out += (seps[i] if seps else "::") + quote_name(seg)
        return out


def separators_for(path) -> list[str]:
    """`.` after a usage (feature chain), `::` after a package or definition."""
    return ["." if getattr(el, "is_usage", False) else "::" for el in path[:-1]]


# Expression operator -> rendering precedence (higher binds tighter). Mirrors
# the grammar's rule nesting in `syntax.py` exactly: an operator may render
# its operand bare only if the operand's precedence is not lower than what
# that position requires, or the text would parse back into a different tree.
_EXPR_PREC = {
    "num": 100, "string": 100, "boolean": 100, "ref": 100, "seq": 100, "call": 100,
    "arrow": 85, "neg": 80, "mul": 70, "div": 70, "mod": 70, "add": 60, "sub": 60,
    "cmp": 50, "not": 40, "and": 30, "xor": 30, "or": 20,
}
_EXPR_BINOPS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%",
               "and": "and", "or": "or", "xor": "xor"}


@dataclass(eq=False)
class Expr:
    """A general expression (arithmetic, comparison, boolean, invocation,
    `->` navigation, collection literal), used wherever SysML v2 allows more
    than a bare literal or reference: attribute/parameter values and the
    result expression of a `constraint`/`calc` body.

    Leaves (`num`, `string`, `boolean`, `ref`) mirror `Value`'s own literal
    kinds; everything else holds its operands in `args`.
    """

    op: str
    args: list["Expr"] = field(default_factory=list)
    text: str | None = None  # literal text, or the comparison operator ("==", ...)
    ref: Ref | None = None  # leaf reference, or the callee of a call/arrow
    name: str | None = None  # member name of a `->` navigation
    unit: Ref | None = None  # unit on a `num` leaf

    @property
    def prec(self) -> int:
        return _EXPR_PREC[self.op]

    def refs(self):
        if self.ref is not None:
            yield self.ref
        for a in self.args:
            yield from a.refs()

    def _at(self, min_prec: int) -> str:
        s = self.render()
        return f"({s})" if self.prec < min_prec else s

    def render(self) -> str:
        if self.op == "num":
            out = self.text
            return out + (f" [{self.unit.render()}]" if self.unit is not None else "")
        if self.op == "string":
            body = self.text.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{body}"'
        if self.op == "boolean":
            return self.text
        if self.op == "ref":
            return self.ref.render()
        if self.op == "seq":
            return "(" + ", ".join(a.render() for a in self.args) + ")"
        if self.op == "neg":
            return "-" + self.args[0]._at(self.prec)
        if self.op == "not":
            return "not " + self.args[0]._at(self.prec)
        if self.op == "cmp":
            left, right = self.args
            return f"{left._at(self.prec)} {self.text} {right._at(self.prec + 1)}"
        if self.op in _EXPR_BINOPS:
            left, right = self.args
            sym = _EXPR_BINOPS[self.op]
            return f"{left._at(self.prec)} {sym} {right._at(self.prec + 1)}"
        if self.op == "call":
            return self.ref.render() + "(" + ", ".join(a.render() for a in self.args) + ")"
        if self.op == "arrow":
            base, args = self.args[0], self.args[1:]
            return f"{base._at(self.prec)}->{self.name}(" + ", ".join(a.render() for a in args) + ")"
        raise ValueError(f"unknown expr op {self.op!r}")


@dataclass(eq=False)
class Value:
    """A literal, reference or general expression assigned to a usage (`= ...`)."""

    kind: str  # "number" | "string" | "boolean" | "ref" | "expr"
    text: str  # number text, raw string content, "true"/"false"
    ref: Ref | None = None
    unit: Ref | None = None
    expr: Expr | None = None  # set when kind == "expr"

    def render(self) -> str:
        if self.kind == "expr":
            return self.expr.render()
        if self.kind == "string":
            body = self.text.replace("\\", "\\\\").replace('"', '\\"')
            out = f'"{body}"'
        elif self.kind == "ref":
            out = self.ref.render()
        else:
            out = self.text
        if self.unit is not None:
            out += f" [{self.unit.render()}]"
        return out

    def to_json(self):
        if self.kind == "expr":
            return {"kind": "expression", "value": self.expr.render()}
        if self.kind == "number":
            num = float(self.text)
            py = int(num) if num.is_integer() and "." not in self.text and "e" not in self.text.lower() else num
            data = {"kind": "number", "value": py}
        elif self.kind == "boolean":
            data = {"kind": "boolean", "value": self.text == "true"}
        elif self.kind == "ref":
            data = {"kind": "ref", "value": str(self.ref)}
        else:
            data = {"kind": "string", "value": self.text}
        if self.unit is not None:
            data["unit"] = str(self.unit)
        return data

    def refs(self):
        if self.kind == "ref" and self.ref is not None:
            yield "value", self.ref
        elif self.kind == "expr" and self.expr is not None:
            for r in self.expr.refs():
                yield "value", r


class Node:
    owner: "Element | None"
    document: "Document | None"

    def namespace(self) -> "Element | None":
        """The namespace in which this node's references are resolved."""
        return self.owner


@dataclass(eq=False)
class Element(Node):
    keyword: str  # "package", "part", "use case", ... ("" for a keyword-less usage)
    is_definition: bool
    name: str | None = None
    abstract: bool = False
    direction: str | None = None
    types: list[Ref] = field(default_factory=list)  # `:`  (FeatureTyping)
    specializes: list[Ref] = field(default_factory=list)  # `:>` (Subclassification / Subsetting)
    redefines: list[Ref] = field(default_factory=list)  # `:>>`
    references: list[Ref] = field(default_factory=list)  # `::>` (ReferenceSubsetting)
    multiplicity: str | None = None
    multiplicity_props: list[str] = field(default_factory=list)  # "ordered", "nonunique"
    value: Value | None = None
    value_op: str = "="  # "=", ":=", "default =", "default :="
    visibility: str | None = None
    short_name: str | None = None  # `<'1.1'>`
    modifiers: list[str] = field(default_factory=list)  # see MODIFIERS (excluding abstract)
    metadata: list[Ref] = field(default_factory=list)  # prefix annotations `#Safety`
    about: list[Ref] = field(default_factory=list)  # metadata usages: `about x, y`
    library_kind: str | None = None  # packages: "library" | "standard library"
    at_shorthand: bool = False  # metadata usage written `@T`
    doc: str | None = None
    members: list[Node] = field(default_factory=list)
    owner: "Element | None" = None
    document: "Document | None" = None
    library: bool = False  # built-in, read-only

    @property
    def kind(self) -> str:
        if self.keyword == "" and "ref" in self.modifiers:
            return "ReferenceUsage"
        if self.is_package and self.library_kind:
            return "LibraryPackage"
        return KIND_BY_KEYWORD[(self.keyword, self.is_definition)]

    @property
    def is_ref(self) -> bool:
        return "ref" in self.modifiers

    @property
    def is_package(self) -> bool:
        return self.keyword == "package"

    @property
    def is_usage(self) -> bool:
        return not self.is_definition and not self.is_package

    @property
    def effective_name(self) -> str | None:
        if self.name is not None:
            return self.name
        if self.redefines:
            return self.redefines[0].segments[-1]
        return None

    def element_members(self):
        return [m for m in self.members if isinstance(m, Element)]

    def namespace(self):
        return self

    def ancestors(self):
        node = self.owner
        while node is not None:
            yield node
            node = node.owner

    def path_names(self) -> list[str]:
        names = [a.effective_name or "?" for a in reversed(list(self.ancestors()))]
        names.append(self.effective_name or "?")
        return names

    def walk(self):
        """Yield every node under (and including) this element, depth-first."""
        yield self
        for m in self.members:
            if isinstance(m, Element):
                yield from m.walk()
            else:
                yield m

    def refs(self):
        """(role, Ref) pairs held directly by this element."""
        for r in self.types:
            yield "type", r
        for r in self.specializes:
            yield "specializes", r
        for r in self.redefines:
            yield "redefines", r
        for r in self.references:
            yield "references", r
        for r in self.metadata:
            yield "metadata", r
        for r in self.about:
            yield "about", r
        if self.value is not None:
            yield from self.value.refs()


@dataclass(eq=False)
class Relationship(Node):
    """`connect a to b;`, `satisfy R by x;`, `allocate a to b;`,
    `flow [of Item] [from] a to b;`, `dependency [from] a to b;`.

    For every kind, `source` is the element the relation goes *from* in the
    graph and `target` the one it goes *to*: for `satisfy R by x`, the graph
    edge is `x -SATISFIES-> R`, so `source` is `x` and `target` is `R`.
    """

    kind: str
    source: Ref
    target: Ref
    metadata: list[Ref] = field(default_factory=list)  # prefix annotations `#Safety`
    item_type: Ref | None = None  # `flow`'s `of <Item>`
    owner: "Element | None" = None
    document: "Document | None" = None

    def refs(self):
        yield "source", self.source
        yield "target", self.target
        for r in self.metadata:
            yield "metadata", r
        if self.item_type is not None:
            yield "item_type", self.item_type

    @property
    def relation_type(self) -> str:
        return RELATION_TYPE_BY_STATEMENT[self.kind]


@dataclass(eq=False)
class Import(Node):
    """`import A::B::*;`. `path` excludes the wildcard."""

    path: list[str]
    wildcard: str | None = None  # None | "*" | "**"
    visibility: str | None = None  # None | "private" | "public"
    owner: "Element | None" = None
    document: "Document | None" = None

    def render_path(self) -> str:
        parts = [quote_name(s) for s in self.path]
        if self.wildcard:
            parts.append(self.wildcard)
        return "::".join(parts)

    def path_ref(self) -> Ref:
        """The import path as a Ref (cached so resolutions can be memoized)."""
        cached = self.__dict__.get("_path_ref")
        if cached is None or cached.segments != self.path:
            cached = Ref(list(self.path))
            self.__dict__["_path_ref"] = cached
        return cached

    def refs(self):
        if self.path:
            yield "import", self.path_ref()


@dataclass(eq=False)
class Alias(Node):
    """`alias Short for Some::Long::Name;`: another name for an element."""

    name: str
    target: Ref
    short_name: str | None = None
    visibility: str | None = None
    doc: str | None = None
    owner: "Element | None" = None
    document: "Document | None" = None

    @property
    def effective_name(self):
        return self.name

    def refs(self):
        yield "alias", self.target


@dataclass(eq=False)
class ExprStatement(Node):
    """A bare result expression in a `constraint`/`calc` body, e.g.
    `constraint { massActual <= massReqd }`: the value of the last
    expression is the definition's implicit result, so (unlike every other
    statement) it is not terminated by `;`."""

    expr: Expr
    owner: "Element | None" = None
    document: "Document | None" = None

    def refs(self):
        for r in self.expr.refs():
            yield "value", r


@dataclass(eq=False)
class Comment(Node):
    """`comment [name] [about a, b] /* text */` (plain `/* */` comments are not kept)."""

    text: str
    name: str | None = None
    short_name: str | None = None
    about: list[Ref] = field(default_factory=list)
    visibility: str | None = None
    owner: "Element | None" = None
    document: "Document | None" = None

    def refs(self):
        for r in self.about:
            yield "about", r


@dataclass(eq=False)
class Document:
    """One `.sysml` file: its top-level members."""

    path: str
    members: list[Node] = field(default_factory=list)
    library: bool = False

    def walk(self):
        for m in self.members:
            if isinstance(m, Element):
                yield from m.walk()
            else:
                yield m


def node_refs(node: Node):
    return list(node.refs())
