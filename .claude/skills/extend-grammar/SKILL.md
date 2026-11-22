---
name: extend-grammar
description: Checklist for adding support for a new SysML v2 textual-notation construct (a new keyword/statement) to the parser. Use when the user asks to support more SysML v2 syntax, e.g. bind, flow, perform, succession, or constraint expressions.
---

# Extending the supported SysML v2 subset

The text is the source of truth and must round-trip (`parse → serialize → parse` gives the same tree), so a new construct touches the whole chain. Work through these in order:

1. **Grammar** — `GRAMMAR` in `sysml_copilot/syntax.py` (Lark, LALR). Keyword literals in quotes, `name` for identifiers, `ref` for references (`A::B.c`). New element keywords that follow the generic `<kw> def Name` / `<kw> name : Type` shape only need adding to `ELEMENT_KEYWORDS` and `_KIND_STEMS` in `model.py` (plus `COMPATIBLE_TYPING`). Other statements need a rule and an entry in `member`. LALR is strict: run the tests right away to catch conflicts.

2. **Tree** — represent it in `model.py`: an `Element` field, or a new `Relationship` kind (add it to `RELATIONSHIP_KINDS` and `RELATION_TYPE_BY_STATEMENT`). Store references as `Ref` objects and expose them through the node's `refs()`. That is what makes resolution, validation, rename/move rewriting and cascade delete work for free.

3. **Builder and serializer** — `_build` (tree → model) and `element_header` / `render_statement` (model → text) in `syntax.py`. The serialized form must parse back to the same tree.

4. **Resolution and relations** — if it creates a new graph relation, emit an `Edge` in `Workspace._compute_edges`. Add a validation rule in `_validate` if misuse is plausible. Decide whether the relation propagates impact; if so, add it to `IMPACT_RELATION_TYPES` (operations.py) and `IMPACT_RELATIONSHIP_TYPES` (query.py), plus `ALLOWED_RELATION_TYPES` in ingest.py.

5. **Operations** — if agents need to create it, extend `create_element` / `add_relationship` (or add an operation, then expose it in `mcp_server.py` with a precise tool description).

6. **Tests** — add a round-trip case in `tests/test_syntax.py`, a resolution/relation case in `tests/test_workspace.py`, and an operation case if you touched step 5. Run `venv/bin/python -m pytest` and `venv/bin/mbse -m data check`.

Keep it grounded in an actual example model: add the construct to `data/sample.sysml` (in canonical form — run `venv/bin/mbse -m data fmt`) or a new file under `data/`.
