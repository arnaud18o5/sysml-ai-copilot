"""SysML v2 textual notation (supported subset): parsing and serialization.

`parse_document(text, path)` turns text into a `model.Document` tree and
`serialize_document(doc)` turns a tree back into canonical text. The two are
designed as a round trip: `parse(serialize(parse(t)))` yields the same tree,
and serializing an already-canonical file reproduces it byte for byte.

Known lossy points: `//` line comments and free-standing `/* */` block
comments are dropped (only `doc /* ... */` is kept), and formatting is
normalized (indentation, blank lines, `::` vs `.` in feature references).
"""

from __future__ import annotations

from lark import Lark, Token, Tree
from lark.exceptions import UnexpectedCharacters, UnexpectedEOF, UnexpectedInput, UnexpectedToken

from .model import (
    ELEMENT_KEYWORDS,
    Document,
    Element,
    Import,
    Ref,
    Relationship,
    Value,
    quote_name,
)

_KIND_ALTERNATIVES = " | ".join(
    " ".join(f'"{w}"' for w in kw.split()) + (' "case"' if kw in ("verification", "analysis") else "")
    for kw in ELEMENT_KEYWORDS
)

GRAMMAR = rf"""
    start: member*

    ?body: "{{" member* "}}" -> block
         | ";"               -> empty

    ?member: package
           | definition
           | usage
           | connect_stmt
           | satisfy_stmt
           | allocate_stmt
           | import_stmt
           | doc_stmt

    package: "package" name body

    definition: ABSTRACT? kind "def" name specialization? body
    specialization: (":>" | "specializes") ref ("," ref)*

    usage: ABSTRACT? direction? kind name? multiplicity? typing? multiplicity? subsetting? redefinition? value? body
    typing: ":" ref ("," ref)*
    subsetting: (":>" | "subsets") ref ("," ref)*
    redefinition: (":>>" | "redefines") ref ("," ref)*
    multiplicity: "[" MULT_BOUND (".." MULT_BOUND)? "]"
    MULT_BOUND: /[0-9]+/ | "*"

    value: "=" expr
    ?expr: SIGNED_NUMBER unit? -> num
         | ESCAPED_STRING      -> string
         | "true"              -> true
         | "false"             -> false
         | ref                 -> refval
    unit: "[" ref "]"

    !kind: {_KIND_ALTERNATIVES}
    !direction: "in" | "out" | "inout"
    ABSTRACT: "abstract"

    connect_stmt: "connect" ref "to" ref ";"
    satisfy_stmt: "satisfy" "requirement"? ref "by" ref ";"
    allocate_stmt: "allocate" ref "to" ref ";"

    import_stmt: VISIBILITY? "import" import_path ";"
    VISIBILITY: "private" | "public"
    import_path: name (DCOLON name)* (DCOLON WILDCARD)?
               | WILDCARD
    WILDCARD: "**" | "*"

    doc_stmt: DOC

    ref: name ((DCOLON | DOT) name)*
    DCOLON: "::"
    DOT: "."
    ?name: NAME | QUOTED_NAME

    DOC.2: /doc\s*\/\*(.|\n)*?\*\//
    NAME: /[^\W\d]\w*/
    QUOTED_NAME: /'([^'\\]|\\.)*'/
    LINE_COMMENT: /\/\/[^\n]*/
    BLOCK_COMMENT: /\/\*(.|\n)*?\*\//

    %import common.WS
    %import common.SIGNED_NUMBER
    %import common.ESCAPED_STRING
    %ignore WS
    %ignore LINE_COMMENT
    %ignore BLOCK_COMMENT
"""

_parser = Lark(GRAMMAR, parser="lalr", propagate_positions=True)
_ref_parser = Lark(GRAMMAR.replace("start: member*", "start: ref"), parser="lalr")
_value_parser = Lark(GRAMMAR.replace("start: member*", "start: expr"), parser="lalr")


class SysmlSyntaxError(Exception):
    """Raised when text isn't valid SysML v2 (supported subset) syntax.

    Exposes `line`, `column`, `context` (a source snippet with a caret) and
    `expected` (a list of tokens the parser would have accepted), so callers
    can show a diagnosable message instead of a raw Lark traceback.
    """

    def __init__(self, message, line=None, column=None, context=None, expected=None, path=None):
        super().__init__(message)
        self.line = line
        self.column = column
        self.context = context
        self.expected = expected or []
        self.path = path

    def to_json(self):
        return {
            "code": "syntax_error",
            "message": str(self),
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "context": self.context,
            "expected": self.expected,
        }


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def unescape_name(raw: str) -> str:
    """`'a\\'b'` -> `a'b`; restricted names are returned unchanged."""
    raw = str(raw)
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        body = raw[1:-1]
        out, i = [], 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", "b": "\b", "f": "\f", "r": "\r"}.get(nxt, nxt))
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)
    return raw


def _unescape_string(raw: str) -> str:
    body = raw[1:-1]
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def clean_doc(raw: str) -> str:
    """Extract the text of `doc /* ... */`, stripping javadoc-style stars."""
    start = raw.index("/*") + 2
    body = raw[start:-2]
    if body.startswith("*"):
        body = body[1:]
    if body.endswith("*"):
        body = body[:-1]
    lines = body.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("* "):
            stripped = stripped[2:]
        elif stripped == "*":
            stripped = ""
        cleaned.append(stripped)
    while cleaned and not cleaned[0]:
        cleaned.pop(0)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)


def _ref(tree: Tree) -> Ref:
    return Ref([unescape_name(t) for t in tree.children if not (isinstance(t, Token) and t.type in ("DCOLON", "DOT"))])


def _refs(tree: Tree) -> list[Ref]:
    return [_ref(c) for c in tree.children if isinstance(c, Tree) and c.data == "ref"]


def _value(expr: Tree) -> Value:
    if expr.data == "num":
        unit = None
        if len(expr.children) > 1:
            unit = _ref(expr.children[1].children[0])
        return Value("number", str(expr.children[0]), unit=unit)
    if expr.data == "string":
        return Value("string", _unescape_string(str(expr.children[0])))
    if expr.data == "true":
        return Value("boolean", "true")
    if expr.data == "false":
        return Value("boolean", "false")
    if expr.data == "refval":
        return Value("ref", "", ref=_ref(expr.children[0]))
    raise ValueError(f"unexpected value node {expr.data}")


def _kind_keyword(tree: Tree) -> str:
    words = [str(t) for t in tree.children]
    if words[-1] == "case" and words[0] in ("verification", "analysis"):
        return words[0]
    return " ".join(words)


def _attach_body(elem: Element, body, document):
    if isinstance(body, Tree) and body.data == "block":
        for child in body.children:
            _add_member(elem, _build(child, document), document)


def _add_member(owner, node, document):
    if node is None:
        return
    if isinstance(node, str):  # doc text
        owner.doc = node
        return
    node.owner = owner if isinstance(owner, Element) else None
    node.document = document
    owner.members.append(node)


def _build(tree: Tree, document):
    data = tree.data
    if data == "package":
        name_tok, body = tree.children
        elem = Element("package", False, name=unescape_name(name_tok))
        _attach_body(elem, body, document)
        return elem

    if data == "definition":
        children = list(tree.children)
        abstract = False
        if isinstance(children[0], Token) and children[0].type == "ABSTRACT":
            abstract = True
            children.pop(0)
        kind_tree = children.pop(0)
        name_tok = children.pop(0)
        body = children.pop()
        elem = Element(_kind_keyword(kind_tree), True, name=unescape_name(name_tok), abstract=abstract)
        for c in children:
            if isinstance(c, Tree) and c.data == "specialization":
                elem.specializes = _refs(c)
        _attach_body(elem, body, document)
        return elem

    if data == "usage":
        children = list(tree.children)
        body = children.pop()
        elem = Element("part", False)
        for c in children:
            if isinstance(c, Token):
                if c.type == "ABSTRACT":
                    elem.abstract = True
                else:
                    elem.name = unescape_name(c)
            elif c.data == "kind":
                elem.keyword = _kind_keyword(c)
            elif c.data == "direction":
                elem.direction = str(c.children[0])
            elif c.data == "typing":
                elem.types = _refs(c)
            elif c.data == "subsetting":
                elem.specializes = _refs(c)
            elif c.data == "redefinition":
                elem.redefines = _refs(c)
            elif c.data == "multiplicity":
                if elem.multiplicity is not None:
                    raise SysmlSyntaxError(
                        "multiplicity given twice",
                        line=c.meta.line, column=c.meta.column,
                    )
                elem.multiplicity = "..".join(str(t) for t in c.children)
            elif c.data == "value":
                elem.value = _value(c.children[0])
        _attach_body(elem, body, document)
        return elem

    if data == "connect_stmt":
        a, b = tree.children
        return Relationship("connect", _ref(a), _ref(b))

    if data == "satisfy_stmt":
        req, by = tree.children
        return Relationship("satisfy", source=_ref(by), target=_ref(req))

    if data == "allocate_stmt":
        a, b = tree.children
        return Relationship("allocate", _ref(a), _ref(b))

    if data == "import_stmt":
        children = list(tree.children)
        visibility = None
        if isinstance(children[0], Token) and children[0].type == "VISIBILITY":
            visibility = str(children.pop(0))
        path_tree = children[0]
        segments, wildcard = [], None
        for t in path_tree.children:
            if isinstance(t, Token) and t.type == "WILDCARD":
                wildcard = str(t)
            elif isinstance(t, Token) and t.type == "DCOLON":
                continue
            else:
                segments.append(unescape_name(t))
        return Import(segments, wildcard=wildcard, visibility=visibility)

    if data == "doc_stmt":
        return clean_doc(str(tree.children[0]))

    raise ValueError(f"unexpected node {data}")


def _expected_tokens(exc) -> list[str]:
    """Human-readable tokens the parser would have accepted at the error."""
    names = set()
    try:
        names = set(exc.interactive_parser.accepts())
    except Exception:
        names = set(getattr(exc, "expected", None) or getattr(exc, "allowed", None) or [])
    out = []
    for name in sorted(names):
        if name == "$END":
            out.append("end of file")
            continue
        try:
            pattern = _parser.get_terminal(name).pattern
        except KeyError:
            out.append(name)
            continue
        if pattern.type == "str":
            out.append(f"'{pattern.value}'")
        else:
            out.append(name.lower().replace("_", " "))
    return sorted(set(out))


def _syntax_error(exc: UnexpectedInput, text: str, path=None) -> SysmlSyntaxError:
    line = getattr(exc, "line", None)
    column = getattr(exc, "column", None)
    try:
        context = exc.get_context(text)
    except Exception:  # pragma: no cover - defensive
        context = None
    expected = _expected_tokens(exc)
    if isinstance(exc, UnexpectedEOF) or (isinstance(exc, UnexpectedToken) and exc.token.type == "$END"):
        message = "unexpected end of input (missing '}' or ';'?)"
    elif isinstance(exc, UnexpectedToken):
        message = f"unexpected '{exc.token}'"
        if expected:
            message += f" (expected one of: {', '.join(expected[:10])})"
    elif isinstance(exc, UnexpectedCharacters):
        message = f"unexpected character {text[exc.pos_in_stream]!r}" if exc.pos_in_stream < len(text) else "unexpected character"
    else:  # pragma: no cover
        message = str(exc)
    where = f" at line {line}, column {column}" if isinstance(line, int) and line > 0 else ""
    return SysmlSyntaxError(f"{message}{where}", line=line, column=column, context=context,
                            expected=expected, path=path)


def parse_document(text: str, path: str = "<memory>") -> Document:
    try:
        tree = _parser.parse(text)
    except UnexpectedInput as exc:
        raise _syntax_error(exc, text, path) from exc
    except SysmlSyntaxError as exc:
        exc.path = path
        raise
    doc = Document(path)
    for child in tree.children:
        node = _build(child, doc)
        if isinstance(node, str):
            # A top-level `doc` has no owner to attach to: ignore it.
            continue
        node.owner = None
        node.document = doc
        doc.members.append(node)
    return doc


def parse_ref(text: str) -> Ref:
    """Parse `A.b.c` / `A::B` into a `Ref`. Raises SysmlSyntaxError."""
    try:
        tree = _ref_parser.parse(text.strip())
    except UnexpectedInput as exc:
        raise _syntax_error(exc, text) from exc
    return _ref(tree.children[0])


def parse_value(text: str) -> Value:
    """Parse a value expression (`3.5`, `"abc"`, `true`, `60 [L]`, `Pkg.x`)."""
    try:
        tree = _value_parser.parse(text.strip())
    except UnexpectedInput as exc:
        raise _syntax_error(exc, text) from exc
    return _value(tree.children[0])


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

INDENT = "    "


def _render_doc(doc: str, indent: str) -> list[str]:
    if "\n" not in doc:
        return [f"{indent}doc /* {doc} */"]
    lines = [f"{indent}doc /*"]
    for line in doc.split("\n"):
        lines.append(f"{indent} * {line}".rstrip())
    lines.append(f"{indent} */")
    return lines


def _render_refs(refs) -> str:
    return ", ".join(str(r) for r in refs)


def element_header(elem: Element) -> str:
    """The declaration line of an element, without its body."""
    parts = []
    if elem.abstract:
        parts.append("abstract")
    if elem.is_package:
        return f"package {quote_name(elem.name)}"
    if elem.direction:
        parts.append(elem.direction)
    keyword = elem.keyword
    if keyword in ("verification", "analysis"):
        keyword += " case"
    parts.append(keyword)
    if elem.is_definition:
        parts.append("def")
        parts.append(quote_name(elem.name))
        if elem.specializes:
            parts.append(":> " + _render_refs(elem.specializes))
        return " ".join(parts)
    decl = quote_name(elem.name) if elem.name is not None else ""
    if elem.types:
        decl += (" : " if decl else ": ") + _render_refs(elem.types)
    if elem.multiplicity is not None:
        decl += f"[{elem.multiplicity}]"
    if decl:
        parts.append(decl)
    if elem.specializes:
        parts.append(":> " + _render_refs(elem.specializes))
    if elem.redefines:
        parts.append(":>> " + _render_refs(elem.redefines))
    if elem.value is not None:
        parts.append("= " + elem.value.render())
    return " ".join(parts)


def render_statement(node) -> str:
    if isinstance(node, Relationship):
        if node.kind == "connect":
            return f"connect {node.source} to {node.target};"
        if node.kind == "satisfy":
            return f"satisfy {node.target} by {node.source};"
        if node.kind == "allocate":
            return f"allocate {node.source} to {node.target};"
    if isinstance(node, Import):
        vis = f"{node.visibility} " if node.visibility else ""
        return f"{vis}import {node.render_path()};"
    raise TypeError(node)


def _has_block(node) -> bool:
    return isinstance(node, Element) and (bool(node.members) or node.doc is not None or node.is_package)


def _render_members(members, indent: str, spacious: bool) -> list[str]:
    """Render members; `spacious` puts a blank line between unlike members."""
    out: list[str] = []
    prev = None
    for m in members:
        if prev is not None:
            blank = False
            if spacious:
                blank = not (type(m) is type(prev) and not _has_block(m) and not _has_block(prev)
                             and getattr(m, "kind", None) == getattr(prev, "kind", None))
            else:
                blank = _has_block(m) or _has_block(prev) or type(m) is not type(prev)
            if blank:
                out.append("")
        out.extend(_render_node(m, indent))
        prev = m
    return out


def _render_node(node, indent: str) -> list[str]:
    if not isinstance(node, Element):
        return [indent + render_statement(node)]
    header = indent + element_header(node)
    if not node.members and node.doc is None:
        if node.is_package:
            return [header + " {", indent + "}"]
        return [header + ";"]
    lines = [header + " {"]
    inner = indent + INDENT
    if node.doc is not None:
        lines.extend(_render_doc(node.doc, inner))
        if node.members:
            lines.append("")
    lines.extend(_render_members(node.members, inner, spacious=node.is_package))
    lines.append(indent + "}")
    return lines


def serialize_node(node, indent: str = "") -> str:
    return "\n".join(_render_node(node, indent)) + "\n"


def serialize_document(doc: Document) -> str:
    lines = _render_members(doc.members, "", spacious=True)
    return "\n".join(lines) + "\n" if lines else ""
