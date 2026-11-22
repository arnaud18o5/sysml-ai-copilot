"""Legacy flat parsing API, kept for the Neo4j ingestion pipeline.

`parse_sysml(text)` returns `(elements, relations)` as lists of dicts. It is
now a thin view over the full model (`syntax` + `workspace`), so it supports
everything the editor supports. Elements are identified by qualified name.
Imports are no longer reported as elements (they polluted vector search).
"""

from .syntax import SysmlSyntaxError  # noqa: F401  (re-exported)
from .workspace import Workspace


class SysmlSemanticError(Exception):
    """Raised when the model is syntactically valid but can't be flattened
    unambiguously (e.g. two elements share a qualified name)."""


def workspace_to_graph(ws: Workspace):
    if any(not els[0].library for els in ws.duplicates.values()):
        dup = sorted(ws.duplicates)[0]
        raise SysmlSemanticError(f"duplicate qualified name: {dup}")
    elements = []
    for el in ws.elements():
        eid = ws.id_of(el)
        elements.append({
            "id": eid,
            "kind": el.kind,
            "name": el.effective_name,
            "qualified_name": eid,
            "doc": el.doc,
            "value": el.value.render() if el.value is not None and el.value.kind != "string"
            else (el.value.text if el.value is not None else None),
        })
    known = {e["id"] for e in elements}
    relations = []
    seen = set()
    for edge in ws.edges():
        src, tgt = ws.id_of(edge.source), ws.id_of(edge.target)
        if src not in known or tgt not in known:
            continue  # edges into the built-in library
        key = (src, tgt, edge.type)
        if key in seen:
            continue
        seen.add(key)
        relations.append({"source": src, "target": tgt, "type": edge.type})
    return elements, relations


def parse_sysml(text):
    return workspace_to_graph(Workspace.from_text(text))


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "data/sample.sysml"
    with open(path, encoding="utf-8") as f:
        elements, relations = parse_sysml(f.read())
    print(json.dumps({"elements": elements, "relations": relations}, indent=2))
