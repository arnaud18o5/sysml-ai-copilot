# CLAUDE.md

Context for Claude Code (or any coding agent) working in this repo.

## What this project is

An agent-controllable MBSE workbench for SysML v2 models. `.sysml` text files are the source of truth; `syntax.py` parses them into a model tree (`model.py`), `workspace.py` resolves names / derives relations / validates, and `operations.py` (`ModelService`) exposes deterministic read and write operations. The same operations are served by the MCP server (`mbse-mcp`), the CLI (`mbse`) and the local web workbench (`mbse serve`). The goal is that a *small* model can do MBSE work through these tools. `semantic.py` adds multilingual semantic search (`find_elements`) with a local embedding model. There is no database: everything works on the files, in memory (the former Neo4j pipeline was removed on purpose).

See [README.md](README.md) for the full architecture and usage, and [CONTRIBUTING.md](CONTRIBUTING.md) for the branch/commit/PR workflow.

## Working in this repo

- **`main` is protected.** Always work on a `type/short-description` branch and open a PR — never push to `main` directly.
- **Open the PR, then stop — do not merge it.** Merging `main` requires 1 approving review, and GitHub won't let a PR author approve their own PR. PRs opened via `gh pr create` under the maintainer's authenticated `gh` session count as authored by them, so only an actual human review from @arnaud18o5 (using the admin bypass, since `enforce_admins` is off) can merge. Never call `gh pr merge`.
- **Commit messages and PR titles are Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, etc.), enforced by a local `commit-msg` hook and a CI check on PR titles. Write commits in that format from the start rather than fixing them after the hook rejects them.
- **Code comments, docstrings, and user-facing CLI output are all in English** (maintainer request, PR #26).

## Environment gotcha

The `python` command on this machine is aliased to a non-venv interpreter. After `source venv/bin/activate`, prefer calling `venv/bin/python` explicitly (or `venv/bin/pip`) rather than bare `python`/`pip` when running anything in a non-interactive shell, or the wrong interpreter gets picked up silently.

## Running things

```bash
venv/bin/pip install -e ".[mcp,semantic,dev,eval]"     # once
venv/bin/python -m pytest                              # test suite (must stay green)
venv/bin/mbse -m data check                            # validate the example model
venv/bin/mbse -m data serve                            # web workbench
venv/bin/mbse -m data <operation> key=value ...        # any operation, JSON out
venv/bin/python tools/conformance.py report            # OMG SysML v2 conformance (after `fetch`)
venv/bin/python evals/run.py --oracle                  # agent eval suite sanity (no API)
```

Where the project is going, and how progress is measured: `docs/ROADMAP.md`. Never lower the conformance baseline (`tools/conformance_baseline.json`), and give every new operation eval tasks.

Tests run with `MBSE_SEMANTIC=off` (set in `tests/conftest.py`) and a fake embedder, so they never download the model. `MBSE_TEST_REAL_EMBEDDINGS=1` also runs the real-model test.

## Architecture invariants to preserve

- **Text is the source of truth.** The model tree mirrors the notation, and serialization must be lossless: `tree_signature(parse(serialize(t))) == tree_signature(t)` (checked in tests and by the conformance harness); `data/sample.sysml` is kept in canonical form (a test checks it byte for byte). Never store resolved pointers in the tree: references are `Ref` segment lists resolved by `Workspace`.
- **Elements are identified by qualified name** (`Pkg.Vehicle.engine`, non-identifier segments quoted), not a synthetic UUID — computed in `Workspace._index`. IDs always use `.`, but SysML text must not: render references with `Ref.render()` (keeps `::` vs `.`), never `str(ref)`, and give generated refs separators via `separators_for`.
- **Name lookup inside an element walks through its types and supertypes** (`Workspace._lookup_in`): this is what lets `connect tank.fuelOut to pump.fuelIn` resolve although the ports live on `FuelTank`/`FuelPump`. Don't simplify it away.
- **Several processes may share the model files** (web UI, MCP server, CLI, git). `call()` runs `ModelService.sync()` before every operation: reload if nothing is unsaved, otherwise refuse writes with `conflict`. Don't bypass `call()` in front-ends.
- **Operations are deterministic and transactional.** Every mutation goes through `ModelService._mutate` (snapshot, rollback on error, undo entry, autosave) and must either fully apply or raise `OpError` with a stable `code`. Renames/moves rely on `_capture_refs` + `_reanchor` to rewrite references; new mutations that can change what a name means must use them too. Return plain JSON-able dicts — they are shown verbatim to agents.
- **A new operation is exposed in three places**: `operations.py` (method + the `*_OPERATIONS` catalogue), `mcp_server.py` (typed tool with a precise description — small models depend on it), and tests. The CLI and web API pick it up from the catalogue.
- **Impact analysis deliberately excludes `CONTAINS`** (`operations.IMPACT_RELATION_TYPES`), to avoid flooding results with unrelated siblings. Extend those lists for new relation types; don't add `CONTAINS` back.
- **Embeddings are local (fastembed, no API key)**, model configured in `config.py` (overridable with `MBSE_EMBEDDING_MODEL`). It's a multilingual model because the maintainer queries in French — don't swap to an English-only model. Semantic search must degrade to lexical (never fail) in `auto` mode when the model is unavailable, and an exact name match must always rank first.

## Known limitations (don't treat as bugs to silently fix without discussion)

- Only a subset of SysML v2 textual notation is supported (see README): no expressions/constraint bodies, no behaviour (successions, transitions, `perform`), no `subject`/`bind`/`flow`. Plain comments are dropped on write-back; `doc` and `comment` are kept.
- Name resolution is a pragmatic subset of SysML v2 scoping (no visibility rules, simplified redefinition semantics), with a last-resort global-name fallback reported as a `heuristic_reference` warning.
- Semantic retrieval is only as good as the element names and `doc` text, and the default model is small (a 1 GB multilingual mpnet was tried and wasn't clearly better on the sample). Don't change the default model without checking with the maintainer first.

## Available skills

- `extend-grammar` — checklist for adding a new SysML v2 construct (grammar, tree builder, serializer, resolution, operations, tests).
- `edit-model` — how to inspect and change a SysML v2 model through the `mbse` operations instead of editing `.sysml` text by hand.
- `roadmap-work` — pick the next roadmap item and deliver it with conformance/eval evidence.
