# CLAUDE.md

Context for Claude Code (or any coding agent) working in this repo.

## What this project is

A prototype AI copilot for analyzing SysML v2 models. It parses a subset of the SysML v2 textual notation, loads the result into Neo4j as a graph with a native vector index, and answers natural-language questions (e.g. "what is the impact of element X?") by resolving the query to a graph element via vector search, then traversing relations.

See [README.md](README.md) for the full architecture and usage, and [CONTRIBUTING.md](CONTRIBUTING.md) for the branch/commit/PR workflow.

## Working in this repo

- **`main` is protected.** Always work on a `type/short-description` branch and open a PR — never push to `main` directly.
- **Commit messages and PR titles are Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, etc.), enforced by a local `commit-msg` hook and a CI check on PR titles. Write commits in that format from the start rather than fixing them after the hook rejects them.
- **Code comments and docstrings are in English.** User-facing CLI output strings (the `print()` calls in `query.py`/`ingest.py`) are in French — that's intentional, matches how the maintainer interacts with the tool. Don't "fix" that.

## Environment gotcha

The `python` command on this machine is aliased to a non-venv interpreter. After `source venv/bin/activate`, prefer calling `venv/bin/python` explicitly (or `venv/bin/pip`) rather than bare `python`/`pip` when running anything in a non-interactive shell, or the wrong interpreter gets picked up silently.

## Running the pipeline

```bash
docker compose up -d                                   # start Neo4j
venv/bin/python -m sysml_copilot.ingest data/sample.sysml
venv/bin/python -m sysml_copilot.query "your question here"
```

Use the `run-pipeline` skill for this instead of re-deriving the steps.

## Architecture invariants to preserve

- **Elements are identified by qualified name** (dot path from the package root), not a synthetic UUID. If you change `Element.id` generation, every relation and the Neo4j `MERGE` keys in `ingest.py` need to follow.
- **Reference resolution in `parser.py` (`_Model.resolve`) walks through `TYPED_BY`** when a dotted-path segment isn't a direct child — this is what lets `connect tank.fuelOut to pump.fuelIn` resolve even though ports live on the *type* (`FuelTank`/`FuelPump`), not on the usage instance. Don't simplify this away without checking `connect_stmt` resolution still works on `data/sample.sysml`.
- **`impact_analysis` in `query.py` deliberately excludes `CONTAINS`** from the traversed relationship types, to avoid flooding results with unrelated siblings under the same package. If you add relation types that should count toward impact, extend `IMPACT_RELATIONSHIP_TYPES`, don't just add `CONTAINS` back in.
- **Embeddings are local (fastembed, no API key)**, model configured in `config.py`. It's currently a multilingual model because the maintainer queries in French — don't swap back to an English-only model.

## Known limitations (don't treat as bugs to silently fix without discussion)

- Only a subset of SysML v2 textual notation is supported: no imports, no feature specialization/redefinition.
- Model modification (write-back) is not implemented — read-only ingestion + analysis only, by design (see README).
- Retrieval quality is weak on elements with little/no `doc` text — this is a data/richness problem, not something to solve by changing the embedding model further without checking with the maintainer first.

## Available skills

- `run-pipeline` — starts Neo4j, runs ingestion, runs a query, using the correct venv interpreter.
- `extend-grammar` — checklist for adding a new SysML v2 construct to the parser (grammar rule, walker case, resolver interaction, test against `data/sample.sysml`).
