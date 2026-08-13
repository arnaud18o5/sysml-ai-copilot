---
name: extend-grammar
description: Checklist for adding support for a new SysML v2 textual-notation construct (a new keyword/statement) to the parser. Use when the user asks to support more SysML v2 syntax, e.g. imports, attributes, allocate, or feature redefinition.
---

# Extending the SysML v2 parser

The parser (`sysml_copilot/parser.py`) is a hand-rolled Lark grammar plus a manual tree-walker (`_walk_node`) — it is not a full implementation of the SysML v2 metamodel, just enough to cover the constructs actually in use. Adding a new construct touches four places:

1. **Grammar rule** — add the new production to the `GRAMMAR` string. Follow the existing style: keyword literals in quotes, `NAME` for identifiers, `qualname` for dotted paths. Keep it permissive rather than exactly spec-compliant; this parser optimizes for the subset actually exercised by real models, not full standard coverage.

2. **Add it to `member`** — every top-level construct that can appear inside a `package`/`part def`/etc. body must be listed as an alternative in the `member` rule, or it won't be recognized inside a block.

3. **Walker case** — add a branch to `_walk_node` for `node.data == "your_new_stmt"`. Decide:
   - Does it create an `Element` (call `model.add_element(kind, name, scope_path, doc=...)`)? Give it a clear `kind` string (existing ones: `Package`, `PartDefinition`, `PortDefinition`, `PartUsage`, `PortUsage`, `RequirementDefinition`).
   - Does it create a `Relation` (`model.add_relation(source, target, type_)`)? Reuse an existing relation type (`CONTAINS`, `TYPED_BY`, `CONNECTS_TO`, `SATISFIES`) if it's semantically the same thing, otherwise add a new one — and if you do, check whether `IMPACT_RELATIONSHIP_TYPES` in `sysml_copilot/query.py` should include it (does this relation represent something that should propagate in impact analysis?).
   - Does it reference other elements by dotted path? Use `model.resolve(parts, scope_path)`, not a direct dict lookup — it's what makes cross-scope and type-based references (like `tank.fuelOut`) work.

4. **Test against `data/sample.sysml`** — either extend that file with an example of the new construct, or add a new fixture file under `data/`. Run the `run-pipeline` skill's ingest step and check both `sysml_copilot.parser` output (run it directly: `venv/bin/python -m sysml_copilot.parser data/sample.sysml`) and a relevant `query` to confirm the new element/relation shows up and resolves correctly — don't just check that parsing doesn't throw.

Do not attempt to jump straight to full SysML v2 metamodel compliance (proper scoping rules, feature redefinition semantics, etc.) in one change — extend one construct at a time and keep it grounded in an actual example model.
