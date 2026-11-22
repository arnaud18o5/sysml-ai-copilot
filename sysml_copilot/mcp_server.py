"""MCP server exposing the deterministic model operations as tools.

Run:  mbse-mcp path/to/model_dir_or_file.sysml [--no-autosave]

Any MCP client (Claude Code, Claude Desktop, an agent built on a small
model...) can then inspect and edit the SysML v2 model through typed,
validated tools instead of editing text by hand.
"""

from __future__ import annotations

import argparse
from typing import Annotated, Any, Literal

from pydantic import Field

from .operations import ModelService, call

INSTRUCTIONS = """\
You are editing a SysML v2 model through deterministic tools. Rules:
- Element ids are qualified names joined by '.', e.g. VehicleFuelSystem.Vehicle.engine.
  Quote names containing spaces: Pkg.'Fuel pump'. Feature chains through a part's type
  are valid ids for relationship endpoints: Pkg.Vehicle.tank.fuelOut.
- Never guess an id: use find_elements or get_tree first, then copy ids exactly.
- Every tool returns {"ok": true, ...} or {"ok": false, "error": {code, message, suggestions}}.
  On error, read the message and suggestions, fix the arguments and retry. Failed calls change nothing.
- Each successful change returns "validation": {errors, warnings}. If errors > 0, call validate
  and fix them before continuing.
- Prefer apply_batch for multi-step edits: it is all-or-nothing.
- undo reverts the last change. Changes are saved to disk automatically unless the server
  runs with --no-autosave (then call save).
Typical workflow: summary -> get_tree -> find_elements/get_element -> create/update/... -> validate.
"""

Id = Annotated[str, Field(description="Element id (qualified name), e.g. 'Pkg.Vehicle.engine'")]
OptId = Annotated[str | None, Field(description="Element id (qualified name), or null")]
Kind = Annotated[str, Field(description="Element kind: 'package', '<kw> def' for a definition or '<kw>' for a "
                                        "usage, kw in part, port, attribute, item, action, state, requirement, "
                                        "constraint, interface, connection, allocation, enum, occurrence, calc, "
                                        "use case, verification, analysis, view, viewpoint, concern")]
Value = Annotated[float | int | bool | str | None,
                  Field(description="Number, boolean, or string. Strings like '60 [L]' become a number with "
                                    "unit; other strings become string literals unless value_kind='ref'")]
ValueKind = Annotated[Literal["auto", "number", "string", "boolean", "ref", "expression"] | None,
                      Field(description="How to interpret `value` (default auto)")]


def build_server(service: ModelService):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("sysml-mbse", instructions=INSTRUCTIONS)

    def tool(fn):
        return mcp.tool(name=fn.__name__, description=fn.__doc__)(fn)

    # ------------------------------------------------------------ reading

    @tool
    def summary() -> dict[str, Any]:
        """Overview of the loaded model: documents, root packages, element counts, validation counts."""
        return call(service, "summary")

    @tool
    def get_tree(root: OptId = None,
                 depth: Annotated[int, Field(ge=1, le=10, description="Levels to expand")] = 3) -> dict[str, Any]:
        """Containment tree (packages > definitions > features), from `root` or from all documents."""
        return call(service, "get_tree", {"root": root, "depth": depth})

    @tool
    def find_elements(query: Annotated[str, Field(description="Words to search, e.g. 'fuel pump'")],
                      kind: Annotated[str | None, Field(description="Optional kind filter, e.g. 'part def'")] = None,
                      limit: int = 10) -> dict[str, Any]:
        """Search elements by name (camelCase aware) and documentation. Use it to get exact ids."""
        return call(service, "find_elements", {"query": query, "kind": kind, "limit": limit})

    @tool
    def list_elements(kind: Annotated[str | None, Field(description="Optional kind filter, e.g. 'requirement def'")] = None,
                      owner: OptId = None,
                      recursive: bool = True,
                      name_contains: str | None = None,
                      limit: int = 200) -> dict[str, Any]:
        """List elements, filtered by kind, owner subtree and/or name substring."""
        return call(service, "list_elements", {"kind": kind, "owner": owner, "recursive": recursive,
                                               "name_contains": name_contains, "limit": limit})

    @tool
    def get_element(element: Id, include_source: bool = True) -> dict[str, Any]:
        """Everything about one element: properties, members, inherited features, relations, diagnostics, SysML text."""
        return call(service, "get_element", {"element": element, "include_source": include_source})

    @tool
    def list_relationships(element: OptId = None,
                           type: Annotated[str | None, Field(description="TYPED_BY, SPECIALIZES, SUBSETS, REDEFINES, "
                                                                         "CONNECTS_TO, SATISFIES, ALLOCATES or CONTAINS")] = None,
                           include_containment: bool = False) -> dict[str, Any]:
        """Relations between elements, optionally only those touching `element` or of one `type`."""
        return call(service, "list_relationships", {"element": element, "type": type,
                                                    "include_containment": include_containment})

    @tool
    def impact_analysis(element: Id,
                        max_hops: Annotated[int, Field(ge=1, le=10)] = 3,
                        direction: Literal["dependents", "dependencies", "both"] = "dependents",
                        relation_types: list[str] | None = None) -> dict[str, Any]:
        """What is affected if `element` changes (dependents), or what it relies on (dependencies).
        Each result explains itself with the chain of relations ('because')."""
        return call(service, "impact_analysis", {"element": element, "max_hops": max_hops,
                                                 "direction": direction, "relation_types": relation_types})

    @tool
    def requirements_trace() -> dict[str, Any]:
        """Requirement coverage matrix: each requirement and the elements that satisfy it."""
        return call(service, "requirements_trace")

    @tool
    def validate(severity: Literal["error", "warning", "info"] | None = None) -> dict[str, Any]:
        """Check the model: unresolved/ambiguous references, typing errors, cycles, unsatisfied requirements."""
        return call(service, "validate", {"severity": severity})

    @tool
    def get_source(element: OptId = None, document: str | None = None) -> dict[str, Any]:
        """SysML v2 textual notation of one element, one document, or everything."""
        return call(service, "get_source", {"element": element, "document": document})

    @tool
    def list_kinds() -> dict[str, Any]:
        """Reference card: valid element kinds, relationship kinds, typing rules, built-in types, value rules."""
        return call(service, "list_kinds")

    # ------------------------------------------------------------ writing

    @tool
    def create_element(kind: Kind,
                       name: Annotated[str, Field(description="Element name (any text; quoted automatically)")],
                       owner: Annotated[str | None, Field(description="Id of the owning element; null for top level")] = None,
                       document: Annotated[str | None, Field(description="Document path, only for top-level elements")] = None,
                       type: Annotated[str | list[str] | None, Field(description="Usages only: id of the typing definition, "
                                                                                 "e.g. 'Pkg.Engine' or 'Real'")] = None,
                       value: Value = None,
                       value_kind: ValueKind = None,
                       doc: Annotated[str | None, Field(description="Documentation text")] = None,
                       multiplicity: Annotated[str | None, Field(description="e.g. '1', '0..1', '1..*', '4'")] = None,
                       direction: Literal["in", "out", "inout"] | None = None,
                       specializes: Annotated[str | list[str] | None,
                                              Field(description="Definitions: supertype id(s). Usages: subsetted feature id(s)")] = None,
                       abstract: bool = False) -> dict[str, Any]:
        """Create a package, definition or usage. Returns the new element's id and its SysML text."""
        return call(service, "create_element", {
            "kind": kind, "name": name, "owner": owner, "document": document, "type": type, "value": value,
            "value_kind": value_kind, "doc": doc, "multiplicity": multiplicity, "direction": direction,
            "specializes": specializes, "abstract": abstract})

    @tool
    def update_element(element: Id,
                       name: Annotated[str | None, Field(description="New name; all references are rewritten")] = None,
                       type: Annotated[str | list[str] | None, Field(description="New typing definition id")] = None,
                       value: Value = None,
                       value_kind: ValueKind = None,
                       doc: str | None = None,
                       multiplicity: str | None = None,
                       direction: Literal["in", "out", "inout"] | None = None,
                       specializes: str | list[str] | None = None,
                       abstract: bool | None = None,
                       clear: Annotated[list[Literal["type", "value", "doc", "multiplicity", "direction", "specializes"]] | None,
                                        Field(description="Properties to remove")] = None) -> dict[str, Any]:
        """Change an element. Omitted arguments stay unchanged; list properties to remove in `clear`."""
        return call(service, "update_element", {
            "element": element, "name": name, "type": type, "value": value, "value_kind": value_kind,
            "doc": doc, "multiplicity": multiplicity, "direction": direction, "specializes": specializes,
            "abstract": abstract, "clear": clear})

    @tool
    def delete_element(element: Id,
                       cascade: Annotated[bool, Field(description="Also remove references to it (typings, "
                                                                  "connections, satisfy...)")] = False) -> dict[str, Any]:
        """Delete an element and its members. Without cascade, refuses (listing them) if anything references it."""
        return call(service, "delete_element", {"element": element, "cascade": cascade})

    @tool
    def move_element(element: Id,
                     new_owner: Annotated[str | None, Field(description="Id of the new owner; null for top level")] = None,
                     document: str | None = None) -> dict[str, Any]:
        """Move an element to another owner. References to and from it are rewritten."""
        return call(service, "move_element", {"element": element, "new_owner": new_owner, "document": document})

    @tool
    def add_relationship(kind: Literal["connect", "satisfy", "allocate"],
                         source: Annotated[str, Field(description="connect/allocate: from; satisfy: the satisfying element. "
                                                                  "Feature chains allowed: Pkg.Vehicle.tank.fuelOut")],
                         target: Annotated[str, Field(description="connect/allocate: to; satisfy: the requirement")],
                         owner: OptId = None) -> dict[str, Any]:
        """Add a connect, satisfy or allocate statement (placed in the closest common owner by default)."""
        return call(service, "add_relationship", {"kind": kind, "source": source, "target": target, "owner": owner})

    @tool
    def remove_relationship(kind: Literal["connect", "satisfy", "allocate"], source: str, target: str) -> dict[str, Any]:
        """Remove a connect, satisfy or allocate statement identified by its endpoints."""
        return call(service, "remove_relationship", {"kind": kind, "source": source, "target": target})

    @tool
    def add_import(namespace: Id,
                   path: Annotated[str, Field(description="e.g. 'OtherPkg::*' or 'OtherPkg::Element'")],
                   visibility: Literal["private", "public"] | None = None) -> dict[str, Any]:
        """Add an import statement to a package or definition."""
        return call(service, "add_import", {"namespace": namespace, "path": path, "visibility": visibility})

    @tool
    def remove_import(namespace: Id, path: str) -> dict[str, Any]:
        """Remove an import statement."""
        return call(service, "remove_import", {"namespace": namespace, "path": path})

    @tool
    def add_from_text(text: Annotated[str, Field(description="SysML v2 snippet, e.g. 'part def Wheel { attribute "
                                                             "radius : Real; }'")],
                      owner: OptId = None,
                      document: str | None = None) -> dict[str, Any]:
        """Add elements written in SysML v2 textual notation. Rejected if it has syntax errors or adds validation errors."""
        return call(service, "add_from_text", {"text": text, "owner": owner, "document": document})

    @tool
    def create_document(path: Annotated[str, Field(description="New file path ending in .sysml")],
                        package: Annotated[str | None, Field(description="Optional root package name")] = None) -> dict[str, Any]:
        """Create a new .sysml file in the workspace, optionally with a root package."""
        return call(service, "create_document", {"path": path, "package": package})

    @tool
    def apply_batch(operations: Annotated[list[dict[str, Any]], Field(
            description="List of {\"op\": <create_element|update_element|delete_element|move_element|"
                        "add_relationship|remove_relationship|add_import|remove_import|add_from_text|"
                        "create_document>, \"args\": {...same arguments as the tool...}}")]) -> dict[str, Any]:
        """Apply several changes atomically: either all succeed, or nothing changes and the failing index is reported."""
        return call(service, "apply_batch", {"operations": operations})

    # ------------------------------------------------------------ control

    @tool
    def undo() -> dict[str, Any]:
        """Undo the last change."""
        return call(service, "undo")

    @tool
    def redo() -> dict[str, Any]:
        """Redo the last undone change."""
        return call(service, "redo")

    @tool
    def diff() -> dict[str, Any]:
        """Unified diff between files on disk and the in-memory model (unsaved changes)."""
        return call(service, "diff")

    @tool
    def save() -> dict[str, Any]:
        """Write changed documents to disk."""
        return call(service, "save")

    return mcp


def main(argv=None):
    parser = argparse.ArgumentParser(description="SysML v2 MBSE MCP server (stdio)")
    parser.add_argument("paths", nargs="*", default=["."], help=".sysml files or directories")
    parser.add_argument("--no-autosave", action="store_true", help="keep changes in memory until `save`")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"])
    args = parser.parse_args(argv)
    service = ModelService(args.paths, autosave=not args.no_autosave)
    build_server(service).run(args.transport)


if __name__ == "__main__":
    main()
