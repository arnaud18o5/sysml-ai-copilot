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

import re

from lark import Lark, Token, Tree
from lark.exceptions import UnexpectedCharacters, UnexpectedEOF, UnexpectedInput, UnexpectedToken

from .model import (
    ELEMENT_KEYWORDS,
    MODIFIERS,
    SPECIAL_KEYWORDS,
    Alias,
    Comment,
    Document,
    Element,
    Expr,
    ExprStatement,
    Import,
    Ref,
    Relationship,
    Value,
    quote_name,
)

_KIND_ALTERNATIVES = " | ".join(
    " ".join(f'"{w}"' for w in kw.split())
    for kw in ELEMENT_KEYWORDS if kw not in SPECIAL_KEYWORDS
)

GRAMMAR = rf"""
    start: member*

    ?body: "{{" member* "}}" -> block
         | ";"               -> empty

    member: visibility? modifiers? member_decl
    prefix_metadata: "#" ref
    ?member_decl: package
                | definition
                | metadata_def
                | usage
                | metadata_usage
                | connect_stmt
                | satisfy_stmt
                | allocate_stmt
                | flow_stmt
                | dependency_stmt
                | import_stmt
                | alias_stmt
                | doc_stmt
                | comment_stmt
                | expr_stmt

    // The implicit result of a `constraint`/`calc` body: unlike every other
    // statement, it is a bare expression with no leading keyword and no `;`.
    expr_stmt: expr

    package: library_prefix? "package" short_name? name body
    !library_prefix: "standard" "library" | "library"

    modifiers: (modifier | prefix_metadata)+
    !modifier: "abstract" | "variation" | "variant" | "individual" | "readonly" | "derived"
             | "end" | "constant" | "ref" | "in" | "out" | "inout"

    definition: kind "def" short_name? name specialization* body
    metadata_def: META "def" short_name? name specialization* body
    specialization: (":>" | "specializes") ref ("," ref)*

    usage: kind? short_name? name? usage_spec* value? connector_ends? body
    // `interface foo : I connect a to b;`: a connector's ends given inline
    // on its own usage declaration, instead of (or alongside) a nested
    // `connect` statement in its body.
    connector_ends: "connect" ref "to" ref
    ?usage_spec: typing | subsetting | redefinition | reference_subsetting | multiplicity_part
    typing: (":" | "defined" "by") typed_ref ("," typed_ref)*
    typed_ref: CONJ? ref
    CONJ: "~"
    subsetting: (":>" | "subsets") ref ("," ref)*
    redefinition: (":>>" | "redefines") ref ("," ref)*
    reference_subsetting: ("::>" | "references") ref ("," ref)*
    multiplicity_part: multiplicity mult_prop*
                     | mult_prop+
    multiplicity: "[" MULT_BOUND (".." MULT_BOUND)? "]"
    MULT_BOUND: /[0-9]+/ | "*"
    !mult_prop: "ordered" | "nonunique"

    metadata_usage: (META | AT) metadata_decl about? body
    metadata_decl: (short_name? name (":" | "typed" "by"))? ref
    about: "about" ref ("," ref)*
    META: "metadata"
    AT: "@"

    value: value_op expr
    !value_op: "=" | ":=" | "default" | "default" "=" | "default" ":="

    // Precedence climbing, loosest to tightest; mirrors `model._EXPR_PREC`.
    ?expr: or_expr
    ?or_expr: and_expr
            | or_expr "or" and_expr -> or_
    ?and_expr: not_expr
             | and_expr "and" not_expr -> and_
             | and_expr "xor" not_expr -> xor_
    ?not_expr: cmp_expr
             | "not" not_expr -> not_
    ?cmp_expr: add_expr
             | add_expr "==" add_expr -> eq
             | add_expr "!=" add_expr -> ne
             | add_expr "<=" add_expr -> le
             | add_expr ">=" add_expr -> ge
             | add_expr "<" add_expr -> lt
             | add_expr ">" add_expr -> gt
    ?add_expr: mul_expr
             | add_expr "+" mul_expr -> add
             | add_expr "-" mul_expr -> sub
    ?mul_expr: unary_expr
             | mul_expr "*" unary_expr -> mul
             | mul_expr "/" unary_expr -> div
             | mul_expr "%" unary_expr -> mod
    ?unary_expr: nav_expr
               | "-" unary_expr -> neg
    ?nav_expr: primary
             | nav_expr "->" name "(" arglist? ")" -> arrow
    ?primary: SIGNED_NUMBER unit? -> num
            | ESCAPED_STRING      -> string
            | "true"              -> true
            | "false"             -> false
            | invocation
            | "(" expr ")"                    -> paren
            | "(" expr ("," expr)+ ")"        -> seq
            | ref                 -> refval
    invocation: ref "(" arglist? ")"
    arglist: expr ("," expr)*
    unit: "[" ref "]"

    !kind: {_KIND_ALTERNATIVES}

    connect_stmt: "connect" ref "to" ref ";"
    satisfy_stmt: "satisfy" "requirement"? ref "by" ref ";"
    allocate_stmt: "allocate" ref "to" ref ";"
    // `from` and the item type are both optional: `flow a to b;`,
    // `flow from a to b;` and `flow of Item from a to b;` all occur.
    flow_stmt: "flow" ("of" ref)? flow_ends ";"
    flow_ends: "from" ref "to" ref
             | ref "to" ref
    dependency_stmt: "dependency" ("from" ref | ref) "to" ref ";"

    import_stmt: "import" import_path ";"
    !visibility: "private" | "public" | "protected"
    import_path: name (DCOLON name)* (DCOLON WILDCARD)?
               | WILDCARD
    WILDCARD: "**" | "*"

    alias_stmt: "alias" short_name? name "for" ref body
    short_name: "<" name ">"

    doc_stmt: DOC
    comment_stmt: COMMENT

    ref: name ((DCOLON | DOT) name)*
    DCOLON: "::"
    DOT: "."
    ?name: NAME | QUOTED_NAME

    DOC.2: /doc\s*\/\*(.|\n)*?\*\//
    COMMENT.2: /comment\b(?:(?!\/\*)[^;{{}}])*\/\*(.|\n)*?\*\//
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
    segments, seps = [], []
    for t in tree.children:
        if isinstance(t, Token) and t.type in ("DCOLON", "DOT"):
            seps.append(str(t))
        else:
            segments.append(unescape_name(t))
    return Ref(segments, seps=seps or None)


def _refs(tree: Tree) -> list[Ref]:
    return [_ref(c) for c in tree.children if isinstance(c, Tree) and c.data == "ref"]


_EXPR_BINOP_DATA = {"add", "sub", "mul", "div", "mod", "and_", "or_", "xor_"}
_CMP_SYMS = {"eq": "==", "ne": "!=", "lt": "<", "gt": ">", "le": "<=", "ge": ">="}


def _build_expr_tree(tree: Tree) -> Expr:
    data = tree.data
    if data == "num":
        unit = None
        if len(tree.children) > 1:
            unit = _ref(tree.children[1].children[0])
        return Expr("num", text=str(tree.children[0]), unit=unit)
    if data == "string":
        return Expr("string", text=_unescape_string(str(tree.children[0])))
    if data == "true":
        return Expr("boolean", text="true")
    if data == "false":
        return Expr("boolean", text="false")
    if data == "refval":
        return Expr("ref", ref=_ref(tree.children[0]))
    if data == "paren":
        return _build_expr_tree(tree.children[0])
    if data == "seq":
        return Expr("seq", args=[_build_expr_tree(c) for c in tree.children])
    if data == "neg":
        return Expr("neg", args=[_build_expr_tree(tree.children[0])])
    if data == "not_":
        return Expr("not", args=[_build_expr_tree(tree.children[0])])
    if data in _EXPR_BINOP_DATA:
        op = data[:-1] if data.endswith("_") else data  # and_/or_/xor_ -> and/or/xor
        left, right = (_build_expr_tree(c) for c in tree.children)
        return Expr(op, args=[left, right])
    if data in _CMP_SYMS:
        left, right = (_build_expr_tree(c) for c in tree.children)
        return Expr("cmp", text=_CMP_SYMS[data], args=[left, right])
    if data == "invocation":
        ref_node, args = None, []
        for c in tree.children:
            if c.data == "ref":
                ref_node = _ref(c)
            elif c.data == "arglist":
                args = [_build_expr_tree(a) for a in c.children]
        return Expr("call", ref=ref_node, args=args)
    if data == "arrow":
        base, member_name, args = None, None, []
        for c in tree.children:
            if isinstance(c, Token):
                member_name = unescape_name(c)
            elif c.data == "arglist":
                args = [_build_expr_tree(a) for a in c.children]
            else:
                base = _build_expr_tree(c)
        return Expr("arrow", name=member_name, args=[base, *args])
    raise ValueError(f"unexpected expr node {data}")


def _value(expr: Tree) -> Value:
    """Build a `Value` from a parsed `expr` tree: a bare literal or
    reference collapses to the matching simple kind (unchanged from before
    expressions were supported); anything with an operator keeps the full
    tree under kind "expr"."""
    e = _build_expr_tree(expr)
    if e.op == "num":
        return Value("number", e.text, unit=e.unit)
    if e.op == "string":
        return Value("string", e.text)
    if e.op == "boolean":
        return Value("boolean", e.text)
    if e.op == "ref":
        return Value("ref", "", ref=e.ref)
    return Value("expr", "", expr=e)


def _kind_keyword(tree: Tree) -> str:
    return " ".join(str(t) for t in tree.children)


def _attach_body(elem: Element, body, document):
    if isinstance(body, Tree) and body.data == "block":
        for child in body.children:
            _add_member(elem, _build_member(child, document), document)


def _add_member(owner, node, document):
    if node is None:
        return
    if isinstance(node, str):  # doc text
        owner.doc = node
        return
    node.owner = owner if isinstance(owner, Element) else None
    node.document = document
    owner.members.append(node)


def _build_member(tree: Tree, document):
    """`member: visibility? modifiers? member_decl`."""
    visibility, modifiers, decl = None, None, None
    for c in tree.children:
        if isinstance(c, Tree) and c.data == "visibility":
            visibility = str(c.children[0])
        elif isinstance(c, Tree) and c.data == "modifiers":
            modifiers = c
        else:
            decl = c
    node = _build(decl, document)
    if isinstance(node, (Element, Import, Alias, Comment)):
        node.visibility = visibility
    if modifiers is not None:
        if isinstance(node, Relationship):
            # A relationship (`connect`, `satisfy`, `allocate`) may carry `#`
            # metadata annotations like any other member, but has no other
            # modifiers to apply.
            for m in modifiers.children:
                if m.data != "prefix_metadata":
                    raise SysmlSyntaxError(
                        f"'{str(m.children[0])}' is not allowed on a relationship",
                        line=tree.meta.line, column=tree.meta.column)
                node.metadata.append(_ref(m.children[0]))
            return node
        if not isinstance(node, Element):
            raise SysmlSyntaxError("modifiers and '#' annotations are only allowed on elements",
                                   line=tree.meta.line, column=tree.meta.column)
        _apply_modifiers(node, modifiers)
        if node.is_package and (node.modifiers or node.abstract or node.direction):
            raise SysmlSyntaxError("a package cannot have modifiers",
                                   line=tree.meta.line, column=tree.meta.column)
        if node.is_definition and node.direction:
            raise SysmlSyntaxError(f"a definition cannot have a direction ('{node.direction}')",
                                   line=tree.meta.line, column=tree.meta.column)
    return node


def _apply_modifiers(elem: Element, tree):
    for m in tree.children:
        if m.data == "prefix_metadata":
            elem.metadata.append(_ref(m.children[0]))
            continue
        word = str(m.children[0])
        if word in ("in", "out", "inout"):
            elem.direction = word
        elif word == "abstract":
            elem.abstract = True
        elif word not in elem.modifiers:
            elem.modifiers.append(word)
    elem.modifiers.sort(key=MODIFIERS.index)


def _short_name(tree) -> str:
    return unescape_name(tree.children[0])


def _typed_refs(tree):
    out = []
    for c in tree.children:
        if isinstance(c, Tree) and c.data == "typed_ref":
            conj = any(isinstance(t, Token) and t.type == "CONJ" for t in c.children)
            r = _ref([t for t in c.children if isinstance(t, Tree)][0])
            r.conjugated = conj
            out.append(r)
    return out


def _build(tree: Tree, document):
    data = tree.data
    if data == "package":
        elem = Element("package", False)
        for c in tree.children:
            if isinstance(c, Tree) and c.data == "library_prefix":
                elem.library_kind = " ".join(str(t) for t in c.children)
            elif isinstance(c, Tree) and c.data == "short_name":
                elem.short_name = _short_name(c)
            elif isinstance(c, Token):
                elem.name = unescape_name(c)
        _attach_body(elem, tree.children[-1], document)
        return elem

    if data in ("definition", "metadata_def"):
        elem = Element("metadata" if data == "metadata_def" else "part", True)
        for c in tree.children[:-1]:
            if isinstance(c, Token):
                if c.type != "META":
                    elem.name = unescape_name(c)
            elif c.data == "kind":
                elem.keyword = _kind_keyword(c)
            elif c.data == "short_name":
                elem.short_name = _short_name(c)
            elif c.data == "specialization":
                elem.specializes.extend(_refs(c))
        _attach_body(elem, tree.children[-1], document)
        return elem

    if data == "usage":
        elem = Element("", False)
        connector = None
        for c in tree.children[:-1]:
            if isinstance(c, Token):
                elem.name = unescape_name(c)
            elif c.data == "kind":
                elem.keyword = _kind_keyword(c)
            elif c.data == "short_name":
                elem.short_name = _short_name(c)
            elif c.data == "typing":
                elem.types.extend(_typed_refs(c))
            elif c.data == "subsetting":
                elem.specializes.extend(_refs(c))
            elif c.data == "redefinition":
                elem.redefines.extend(_refs(c))
            elif c.data == "reference_subsetting":
                elem.references.extend(_refs(c))
            elif c.data == "multiplicity_part":
                for part in c.children:
                    if part.data == "multiplicity":
                        if elem.multiplicity is not None:
                            raise SysmlSyntaxError("multiplicity given twice",
                                                   line=part.meta.line, column=part.meta.column)
                        elem.multiplicity = "..".join(str(t) for t in part.children)
                    else:
                        prop = str(part.children[0])
                        if prop not in elem.multiplicity_props:
                            elem.multiplicity_props.append(prop)
            elif c.data == "value":
                op_tree, expr = c.children
                elem.value_op = " ".join(str(t) for t in op_tree.children)
                elem.value = _value(expr)
            elif c.data == "connector_ends":
                if elem.keyword not in ("interface", "connection"):
                    raise SysmlSyntaxError(
                        "inline 'connect ... to ...' is only allowed on an interface or "
                        "connection usage", line=c.meta.line, column=c.meta.column)
                a, b = _refs(c)
                connector = Relationship("connect", a, b)
        elem.multiplicity_props.sort(key=("ordered", "nonunique").index)
        _attach_body(elem, tree.children[-1], document)
        if connector is not None:
            connector.owner, connector.document = elem, document
            elem.members.insert(0, connector)
        return elem

    if data == "metadata_usage":
        elem = Element("metadata", False)
        for c in tree.children[:-1]:
            if isinstance(c, Token):
                elem.at_shorthand = c.type == "AT"
            elif c.data == "metadata_decl":
                for d in c.children:
                    if isinstance(d, Token):
                        elem.name = unescape_name(d)
                    elif d.data == "short_name":
                        elem.short_name = _short_name(d)
                    elif d.data == "ref":
                        elem.types = [_ref(d)]
            elif c.data == "about":
                elem.about = _refs(c)
        _attach_body(elem, tree.children[-1], document)
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

    if data == "flow_stmt":
        item_type, source, target = None, None, None
        for c in tree.children:
            if c.data == "ref":
                item_type = _ref(c)
            elif c.data == "flow_ends":
                a, b = (gc for gc in c.children if isinstance(gc, Tree) and gc.data == "ref")
                source, target = _ref(a), _ref(b)
        return Relationship("flow", source, target, item_type=item_type)

    if data == "dependency_stmt":
        a, b = tree.children
        return Relationship("dependency", _ref(a), _ref(b))

    if data == "import_stmt":
        segments, wildcard = [], None
        for t in tree.children[0].children:
            if isinstance(t, Token) and t.type == "WILDCARD":
                wildcard = str(t)
            elif isinstance(t, Token) and t.type == "DCOLON":
                continue
            else:
                segments.append(unescape_name(t))
        return Import(segments, wildcard=wildcard)

    if data == "alias_stmt":
        short, name, target = None, None, None
        for c in tree.children[:-1]:
            if isinstance(c, Token):
                name = unescape_name(c)
            elif c.data == "short_name":
                short = _short_name(c)
            elif c.data == "ref":
                target = _ref(c)
        alias = Alias(name, target, short_name=short)
        body = tree.children[-1]
        if isinstance(body, Tree) and body.data == "block":
            for child in body.children:
                part = _build_member(child, document)
                if isinstance(part, str):
                    alias.doc = part
                else:
                    raise SysmlSyntaxError("an alias body may only contain a doc comment",
                                           line=child.meta.line, column=child.meta.column)
        return alias

    if data == "expr_stmt":
        return ExprStatement(_build_expr_tree(tree.children[0]))

    if data == "doc_stmt":
        return clean_doc(str(tree.children[0]))

    if data == "comment_stmt":
        return _comment(str(tree.children[0]), tree)

    raise ValueError(f"unexpected node {data}")


_COMMENT_HEAD = re.compile(
    r"^comment\b\s*(?:<(?P<short>'(?:[^'\\]|\\.)*'|[^\W\d]\w*)>\s*)?"
    r"(?P<name>'(?:[^'\\]|\\.)*'|(?!about\b)[^\W\d]\w*)?\s*"
    r"(?:about\s+(?P<about>[^/]*?))?\s*$", re.S)


def _comment(raw: str, tree) -> Comment:
    head = raw[: raw.index("/*")]
    m = _COMMENT_HEAD.match(head.strip())
    if not m:
        raise SysmlSyntaxError(f"cannot read comment header {head.strip()!r}",
                               line=tree.meta.line, column=tree.meta.column)
    about = []
    if m.group("about"):
        for part in m.group("about").split(","):
            about.append(parse_ref(part.strip()))
    return Comment(clean_doc(raw), name=unescape_name(m.group("name")) if m.group("name") else None,
                   short_name=unescape_name(m.group("short")) if m.group("short") else None, about=about)


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
        node = _build_member(child, doc)
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


def _render_text_block(prefix: str, text: str, indent: str) -> list[str]:
    if "\n" not in text:
        return [f"{indent}{prefix} /* {text} */"]
    lines = [f"{indent}{prefix} /*"]
    for line in text.split("\n"):
        lines.append(f"{indent} * {line}".rstrip())
    lines.append(f"{indent} */")
    return lines


def _render_doc(doc: str, indent: str) -> list[str]:
    return _render_text_block("doc", doc, indent)


def _render_refs(refs) -> str:
    return ", ".join(("~" if r.conjugated else "") + r.render() for r in refs)


def _prefix(node) -> list[str]:
    parts = []
    if getattr(node, "visibility", None):
        parts.append(node.visibility)
    for m in getattr(node, "metadata", []) or []:
        parts.append(f"#{m}")
    return parts


def _modifier_words(elem: Element) -> list[str]:
    words = [elem.direction] if elem.direction else []
    if elem.abstract:
        words.append("abstract")
    words.extend(elem.modifiers)
    return words


def element_header(elem: Element) -> str:
    """The declaration line of an element, without its body."""
    parts = _prefix(elem)
    short = f"<{quote_name(elem.short_name)}>" if elem.short_name is not None else None
    if elem.is_package:
        if elem.library_kind:
            parts.append(elem.library_kind)
        parts.append("package")
        if short:
            parts.append(short)
        parts.append(quote_name(elem.name))
        return " ".join(parts)
    parts.extend(_modifier_words(elem))
    keyword = elem.keyword
    if elem.is_definition:
        parts.extend([keyword, "def"])
        if short:
            parts.append(short)
        parts.append(quote_name(elem.name))
        if elem.specializes:
            parts.append(":> " + _render_refs(elem.specializes))
        return " ".join(parts)
    if keyword == "metadata":
        decl = ""
        if elem.name is not None:
            decl = (f"{short} " if short else "") + f"{quote_name(elem.name)} : "
        decl += _render_refs(elem.types)
        if elem.at_shorthand and elem.name is None:
            parts.append("@" + decl)
        else:
            parts.extend(["@" if elem.at_shorthand else "metadata", decl])
        if elem.about:
            parts.append("about " + _render_refs(elem.about))
        return " ".join(parts)
    if keyword:
        parts.append(keyword)
    if short:
        parts.append(short)
    decl = quote_name(elem.name) if elem.name is not None else ""
    if elem.types:
        decl += (" : " if decl else ": ") + _render_refs(elem.types)
    if elem.multiplicity is not None:
        decl += f"[{elem.multiplicity}]"
    if elem.multiplicity_props:
        decl += (" " if decl else "") + " ".join(elem.multiplicity_props)
    if decl:
        parts.append(decl)
    if elem.specializes:
        parts.append(":> " + _render_refs(elem.specializes))
    if elem.redefines:
        parts.append(":>> " + _render_refs(elem.redefines))
    if elem.references:
        parts.append("::> " + _render_refs(elem.references))
    if elem.value is not None:
        parts.append(f"{elem.value_op} " + elem.value.render())
    return " ".join(parts)


def render_statement(node) -> str:
    if isinstance(node, Relationship):
        prefix = _prefix(node)
        pre = " ".join(prefix) + " " if prefix else ""
        if node.kind == "connect":
            return pre + f"connect {node.source.render()} to {node.target.render()};"
        if node.kind == "satisfy":
            return pre + f"satisfy {node.target.render()} by {node.source.render()};"
        if node.kind == "allocate":
            return pre + f"allocate {node.source.render()} to {node.target.render()};"
        if node.kind == "flow":
            of_ = f" of {node.item_type.render()}" if node.item_type is not None else ""
            return pre + f"flow{of_} from {node.source.render()} to {node.target.render()};"
        if node.kind == "dependency":
            return pre + f"dependency from {node.source.render()} to {node.target.render()};"
    if isinstance(node, Import):
        return " ".join(_prefix(node) + [f"import {node.render_path()};"])
    if isinstance(node, Alias):
        short = [f"<{quote_name(node.short_name)}>"] if node.short_name is not None else []
        return " ".join(_prefix(node) + ["alias"] + short + [quote_name(node.name), "for", f"{node.target.render()};"])
    if isinstance(node, Comment):
        return "\n".join(_render_comment(node, ""))
    if isinstance(node, ExprStatement):
        return node.expr.render()
    raise TypeError(node)


def _render_comment(node: Comment, indent: str) -> list[str]:
    head = _prefix(node) + ["comment"]
    if node.short_name is not None:
        head.append(f"<{quote_name(node.short_name)}>")
    if node.name is not None:
        head.append(quote_name(node.name))
    if node.about:
        head.append("about " + _render_refs(node.about))
    return _render_text_block(" ".join(head), node.text, indent)


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
    if isinstance(node, Comment):
        return _render_comment(node, indent)
    if isinstance(node, Alias) and node.doc is not None:
        head = render_statement(node)[:-1]
        return [indent + head + " {", *_render_doc(node.doc, indent + INDENT), indent + "}"]
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


def tree_signature(node):
    """Structural dump of a parsed tree (every field except back-pointers), used
    to check that serialization loses nothing: parse(serialize(t)) == t."""
    import dataclasses

    if isinstance(node, Document):
        return [tree_signature(m) for m in node.members]
    if isinstance(node, list):
        return [tree_signature(x) for x in node]
    if dataclasses.is_dataclass(node):
        out = {"_": type(node).__name__}
        for f in dataclasses.fields(node):
            if f.name in ("owner", "document"):
                continue
            out[f.name] = tree_signature(getattr(node, f.name))
        return out
    return node
