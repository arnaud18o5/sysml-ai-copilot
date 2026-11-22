# CLAUDE.md

Context for Claude Code (or any coding agent) working in this repo.

## What this project is

An agent-controllable MBSE workbench for SysML v2 models. `.sysml` text files are the source of truth; `syntax.py` parses them into a model tree (`model.py`), `workspace.py` resolves names / derives relations / validates, and `operations.py` (`ModelService`) exposes deterministic read and write operations. The same operations are served by the MCP server (`mbse-mcp`), the CLI (`mbse`) and the local web workbench (`mbse serve`). The goal is that a *small* model can do MBSE work through these tools. An optional Neo4j + vector-search pipeline (`ingest.py` / `query.py`) answers natural-language questions.

See [README.md](README.md) for the full architecture and usage, and [CONTRIBUTING.md](CONTRIBUTING.md) for the branch/commit/PR workflow.

## Working in this repo

- **`main` is protected.** Always work on a `type/short-description` branch and open a PR — never push to `main` directly.
- **Open the PR, then stop — do not merge it.** Merging `main` requires 1 approving review, and GitHub won't let a PR author approve their own PR. PRs opened via `gh pr create` under the maintainer's authenticated `gh` session count as authored by them, so only an actual human review from @arnaud18o5 (using the admin bypass, since `enforce_admins` is off) can merge. Never call `gh pr merge`.
- **Commit messages and PR titles are Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, etc.), enforced by a local `commit-msg` hook and a CI check on PR titles. Write commits in that format from the start rather than fixing them after the hook rejects them.
- **Code comments, docstrings, and user-facing CLI output are all in English.** The `print()` calls in `query.py`/`ingest.py` used to be in French; that convention was dropped per maintainer request (PR #26) — write new CLI output in English.

## Environment gotcha

The `python` command on this machine is aliased to a non-venv interpreter. After `source venv/bin/activate`, prefer calling `venv/bin/python` explicitly (or `venv/bin/pip`) rather than bare `python`/`pip` when running anything in a non-interactive shell, or the wrong interpreter gets picked up silently.

## Running things

```bash
venv/bin/pip install -e ".[mcp,dev]"                   # once
venv/bin/python -m pytest                              # test suite (must stay green)
venv/bin/mbse -m data check                            # validate the example model
venv/bin/mbse -m data serve                            # web workbench
venv/bin/mbse -m data <operation> key=value ...        # any operation, JSON out
```

The optional Neo4j pipeline (`docker compose up -d`, then `ingest` / `query`) is covered by the `run-pipeline` skill.

## Architecture invariants to preserve

- **Text is the source of truth.** The model tree mirrors the notation, and `serialize_document(parse_document(t))` must be idempotent; `data/sample.sysml` is kept in canonical form (a test checks it byte for byte). Never store resolved pointers in the tree: references are `Ref` segment lists resolved by `Workspace`.
- **Elements are identified by qualified name** (`Pkg.Vehicle.engine`, non-identifier segments quoted), not a synthetic UUID — computed in `Workspace._index`. The Neo4j `MERGE` keys in `ingest.py` use the same IDs.
- **Name lookup inside an element walks through its types and supertypes** (`Workspace._lookup_in`): this is what lets `connect tank.fuelOut to pump.fuelIn` resolve although the ports live on `FuelTank`/`FuelPump`. Don't simplify it away.
- **Operations are deterministic and transactional.** Every mutation goes through `ModelService._mutate` (snapshot, rollback on error, undo entry, autosave) and must either fully apply or raise `OpError` with a stable `code`. Renames/moves rely on `_capture_refs` + `_reanchor` to rewrite references; new mutations that can change what a name means must use them too. Return plain JSON-able dicts — they are shown verbatim to agents.
- **A new operation is exposed in three places**: `operations.py` (method + the `*_OPERATIONS` catalogue), `mcp_server.py` (typed tool with a precise description — small models depend on it), and tests. The CLI and web API pick it up from the catalogue.
- **Impact analysis deliberately excludes `CONTAINS`** (in `operations.IMPACT_RELATION_TYPES` and `query.IMPACT_RELATIONSHIP_TYPES`), to avoid flooding results with unrelated siblings. Extend those lists for new relation types; don't add `CONTAINS` back.
- **Embeddings are local (fastembed, no API key)**, model configured in `config.py`. It's currently a multilingual model because the maintainer queries in French — don't swap back to an English-only model.

## Known limitations (don't treat as bugs to silently fix without discussion)

- Only a subset of SysML v2 textual notation is supported (see README): no expressions/constraint bodies, no behaviour (successions, transitions, `perform`), no `bind`/`flow`/`ref`/metadata. Plain comments are dropped on write-back; `doc` is kept.
- Name resolution is a pragmatic subset of SysML v2 scoping (no visibility rules, simplified redefinition semantics), with a last-resort global-name fallback reported as a `heuristic_reference` warning.
- Retrieval quality is weak on elements with little/no `doc` text — this is a data/richness problem, not something to solve by changing the embedding model further without checking with the maintainer first.

## Available skills

- `run-pipeline` — starts Neo4j, runs ingestion, runs a query, using the correct venv interpreter.
- `extend-grammar` — checklist for adding a new SysML v2 construct (grammar, tree builder, serializer, resolution, operations, tests).
- `edit-model` — how to inspect and change a SysML v2 model through the `mbse` operations instead of editing `.sysml` text by hand.
