---
name: edit-model
description: Inspect or change a SysML v2 model (add parts, ports, requirements, connections, rename, move, delete, impact analysis) through the deterministic mbse operations instead of editing .sysml text by hand. Use whenever the user asks to model something, change a model, or analyze one.
---

# Editing a SysML v2 model with `mbse`

Prefer the `sysml-mbse` MCP tools when they are available (configured in `.mcp.json`). Otherwise use the CLI. It exposes the same operations and saves after each successful change:

```bash
venv/bin/mbse -m data <operation> key=value ...      # or one JSON object
venv/bin/mbse -m data --dry-run <operation> ...      # show the diff, don't save
```

## Workflow

1. **Orient yourself.** Run `summary`, then `get_tree depth=2`.
2. **Get exact IDs.** Use `find_elements query="..."` or `get_element element=<id>`. Never guess an ID: they are qualified names (`Pkg.Vehicle.engine`; quote odd names: `Pkg.'Fuel tank'`). A port of a part is addressed through the part: `Pkg.Vehicle.tank.fuelOut`.
3. **Change the model.**
   - `create_element`: `kind` is `"part def"` / `"part"` / `"port def"` / `"port"` / `"requirement def"` / `"package"`… (see `list_kinds`), with `owner`, and optional `type`, `value`, `multiplicity`, `direction`, `doc` and `specializes`.
   - `update_element`: renames rewrite every reference. Use `clear=[...]` to remove properties.
   - `add_relationship`: `kind=connect|satisfy|allocate`. For `satisfy`, `target` is the requirement.
   - `move_element` and `delete_element`. A delete is refused if the element is referenced; `cascade=true` also removes the references.
   - `add_from_text` adds a SysML snippet in one go. It is rejected if it adds errors.
   - `apply_batch` runs several operations all-or-nothing.
4. **Check.** Each change returns `validation.errors` and `validation.warnings`. Run `validate` and fix any errors. `undo` reverts the last change.
5. **Analyze.** Use `impact_analysis element=<id>` (the `because` field explains each hit), `requirements_trace` and `list_relationships`.

Errors come back as `{"ok": false, "error": {"code", "message", "suggestions"}}`, and nothing is changed. Read the suggestions, which are often the exact ID you meant, and retry.

Edit the `.sysml` text directly only for constructs the operations don't cover. Afterwards, run `mbse -m data check` and `mbse -m data fmt`.
