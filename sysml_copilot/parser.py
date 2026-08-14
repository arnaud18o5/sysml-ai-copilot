"""Parser for a subset of the SysML v2 textual notation.

Covers: package, part def, port def, part/port usage, connect, requirement def,
satisfy, import. Produces a list of elements and a list of typed relations,
ready to load into a graph.
"""

from lark import Lark, Tree, Token

GRAMMAR = r"""
    start: package

    package: "package" NAME "{" member* "}"

    member: part_def
          | port_def
          | part_usage
          | port_usage
          | connect_stmt
          | requirement_def
          | satisfy_stmt
          | import_stmt

    import_stmt: "private"? "import" import_path ";"
    import_path: NAME ("::" (NAME | WILDCARD))*
    WILDCARD: "**" | "*"

    part_def: "part" "def" NAME ("{" member* "}" | ";")
    port_def: "port" "def" NAME ("{" member* "}" | ";")

    part_usage: "part" NAME ":" qualname ("{" member* "}" | ";")
    port_usage: "port" NAME ":" qualname ";"

    connect_stmt: "connect" qualname "to" qualname ";"

    requirement_def: "requirement" "def" NAME "{" doc_stmt? "}"
    doc_stmt: "doc" DOC_COMMENT

    satisfy_stmt: "satisfy" qualname "by" qualname ";"

    qualname: NAME ("." NAME)*

    NAME: /[A-Za-z_][A-Za-z0-9_]*/ | /'([^'\\]|\\.)*'/
    DOC_COMMENT: /\/\*.*?\*\//s

    %import common.WS
    %ignore WS
    %ignore /\/\/[^\n]*/
    %ignore DOC_COMMENT
"""

_parser = Lark(GRAMMAR, parser="earley")


class Element:
    def __init__(self, id_, kind, name, qualified_name, doc=None):
        self.id = id_
        self.kind = kind
        self.name = name
        self.qualified_name = qualified_name
        self.doc = doc

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "doc": self.doc,
        }


class Relation:
    def __init__(self, source, target, type_):
        self.source = source
        self.target = target
        self.type = type_

    def to_dict(self):
        return {"source": self.source, "target": self.target, "type": self.type}


def _clean_name(raw):
    """Strip the surrounding quotes from a SysML v2 unrestricted name.

    `'system-of-systems'` -> `system-of-systems`. Restricted (unquoted)
    names are returned unchanged.
    """
    raw = str(raw)
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    return raw


def _qualname_str(qualname_tree):
    return ".".join(_clean_name(tok) for tok in qualname_tree.children)


def _import_path_str(import_path_tree):
    return "::".join(str(tok) for tok in import_path_tree.children)


class _Model:
    def __init__(self):
        self.elements = []
        self.relations = []
        self.by_qualified_name = {}
        self._typed_by = {}

    def add_element(self, kind, name, scope_path, doc=None):
        qualified_name = ".".join(scope_path + [name])
        elem = Element(qualified_name, kind, name, qualified_name, doc)
        self.elements.append(elem)
        self.by_qualified_name[qualified_name] = elem
        return elem

    def add_relation(self, source, target, type_):
        self.relations.append(Relation(source, target, type_))
        if type_ == "TYPED_BY":
            self._typed_by[source] = target

    def _resolve_first_segment(self, first, scope_path):
        for depth in range(len(scope_path), -1, -1):
            candidate = ".".join(scope_path[:depth] + [first])
            if candidate in self.by_qualified_name:
                return candidate
        suffix = "." + first
        matches = [
            qn for qn in self.by_qualified_name if qn == first or qn.endswith(suffix)
        ]
        if matches:
            matches.sort(key=lambda qn: -len(qn))
            return matches[0]
        return None

    def resolve(self, ref_parts, scope_path):
        """Resolve a dotted-path reference relative to the current scope.

        The first segment is looked up from the deepest scope outward (or by
        suffix match as a last resort). Subsequent segments are looked up as
        direct children (CONTAINS); if a segment isn't a direct child of a
        usage, resolution continues through the usage's type (TYPED_BY) —
        e.g. `tank.fuelOut` where `tank` is a usage of `FuelTank` and
        `fuelOut` is declared on the type.
        """
        current = self._resolve_first_segment(ref_parts[0], scope_path)
        if current is None:
            return None
        for part in ref_parts[1:]:
            direct = current + "." + part
            if direct in self.by_qualified_name:
                current = direct
                continue
            type_id = self._typed_by.get(current)
            candidate = (type_id + "." + part) if type_id else None
            if candidate and candidate in self.by_qualified_name:
                current = candidate
                continue
            return None
        return current


def _walk_members(members, model, scope_path, container_id):
    for member in members:
        child = member.children[0]
        _walk_node(child, model, scope_path, container_id)


def _walk_node(node, model, scope_path, container_id):
    if node.data == "part_def":
        name = _clean_name(node.children[0])
        body = [c for c in node.children[1:] if isinstance(c, Tree)]
        elem = model.add_element("PartDefinition", name, scope_path)
        if container_id:
            model.add_relation(container_id, elem.id, "CONTAINS")
        _walk_members(body, model, scope_path + [name], elem.id)

    elif node.data == "port_def":
        name = _clean_name(node.children[0])
        body = [c for c in node.children[1:] if isinstance(c, Tree)]
        elem = model.add_element("PortDefinition", name, scope_path)
        if container_id:
            model.add_relation(container_id, elem.id, "CONTAINS")
        _walk_members(body, model, scope_path + [name], elem.id)

    elif node.data == "part_usage":
        name = _clean_name(node.children[0])
        type_ref = _qualname_str(node.children[1])
        body = [c for c in node.children[2:] if isinstance(c, Tree)]
        elem = model.add_element("PartUsage", name, scope_path)
        if container_id:
            model.add_relation(container_id, elem.id, "CONTAINS")
        target = model.resolve(type_ref.split("."), scope_path)
        if target:
            model.add_relation(elem.id, target, "TYPED_BY")
        _walk_members(body, model, scope_path + [name], elem.id)

    elif node.data == "port_usage":
        name = _clean_name(node.children[0])
        type_ref = _qualname_str(node.children[1])
        elem = model.add_element("PortUsage", name, scope_path)
        if container_id:
            model.add_relation(container_id, elem.id, "CONTAINS")
        target = model.resolve(type_ref.split("."), scope_path)
        if target:
            model.add_relation(elem.id, target, "TYPED_BY")

    elif node.data == "connect_stmt":
        left = _qualname_str(node.children[0]).split(".")
        right = _qualname_str(node.children[1]).split(".")
        left_id = model.resolve(left, scope_path)
        right_id = model.resolve(right, scope_path)
        if left_id and right_id:
            model.add_relation(left_id, right_id, "CONNECTS_TO")

    elif node.data == "requirement_def":
        name = _clean_name(node.children[0])
        doc = None
        for c in node.children[1:]:
            if isinstance(c, Tree) and c.data == "doc_stmt":
                raw = str(c.children[0])
                doc = raw[2:-2].strip()
        elem = model.add_element("RequirementDefinition", name, scope_path, doc=doc)
        if container_id:
            model.add_relation(container_id, elem.id, "CONTAINS")

    elif node.data == "satisfy_stmt":
        req_ref = _qualname_str(node.children[0]).split(".")
        by_ref = _qualname_str(node.children[1]).split(".")
        req_id = model.resolve(req_ref, scope_path)
        by_id = model.resolve(by_ref, scope_path)
        if req_id and by_id:
            model.add_relation(by_id, req_id, "SATISFIES")

    elif node.data == "import_stmt":
        path = _import_path_str(node.children[0])
        elem = model.add_element("Import", path, scope_path)
        if container_id:
            model.add_relation(container_id, elem.id, "CONTAINS")


def parse_sysml(text):
    tree = _parser.parse(text)
    package_node = tree.children[0]
    package_name = _clean_name(package_node.children[0])
    members = [c for c in package_node.children[1:] if isinstance(c, Tree)]

    model = _Model()
    pkg_elem = model.add_element("Package", package_name, [])
    _walk_members(members, model, [package_name], pkg_elem.id)

    return (
        [e.to_dict() for e in model.elements],
        [r.to_dict() for r in model.relations],
    )


if __name__ == "__main__":
    import sys
    import json

    path = sys.argv[1] if len(sys.argv) > 1 else "data/sample.sysml"
    with open(path) as f:
        text = f.read()
    elements, relations = parse_sysml(text)
    print(json.dumps({"elements": elements, "relations": relations}, indent=2))
