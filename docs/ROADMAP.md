# Roadmap: an MBSE tool an engineering company can adopt

## Target

An engineering organisation that models systems today in a desktop MBSE tool such as Cameo Systems Modeler wants:

1. **SysML v2 models as text in git.** They get reviewable diffs and branches, and CI validates them.
2. **A workbench engineers can use every day.** This means browsing the model, viewing diagrams, tables and matrices, editing properties, and analysing impact and coverage.
3. **Agents that do real modelling work through deterministic tools,** including cheap, small models. The tools guarantee the model stays well-formed. The agent only has to pick the right operation.
4. **A migration and coexistence path** with the tools already in place: SysML v1 models, requirement tools, document templates.

We are not cloning a desktop tool screen by screen. The differentiators are text and git, agent control, and openness (standard SysML v2, MCP, the OMG SysML v2 API).

## How progress is measured

Every phase has measurable exit criteria. Three numbers are tracked from the start:

| Metric | Tool | Now (2026-11) |
|---|---|---|
| OMG SysML v2 release files that parse / round-trip / resolve | `tools/conformance.py report` | 17 / 17 / 5 of 310 (5.5% / 5.5% / 1.6%) |
| Agent task success, small model | `evals/run.py --model claude-haiku-5-5` | not measured yet (needs an API key) |
| Agent task suite size | `evals/tasks.json` | 14 tasks, all solvable by their reference solution |

Both harnesses are ratchets.
- **Conformance:** the baseline only goes up, and CI fails on any regression.
- **Evals:** every new operation or language feature comes with tasks. A task must be solvable by its reference solution and must fail on an untouched model, and `tests/test_evals.py` enforces both.

## Phase 0: Foundations (done, PR #81)

- Round-trip parser and serializer, multi-file workspace, name resolution, validation.
- Deterministic operations, kept consistent across edits: rename and move rewrite references, cascade delete, atomic batches, undo/redo, conflict detection when several processes share the files.
- MCP server (stdio and HTTP), JSON CLI, and a web workbench with live refresh. Diagrams: internal structure and definitions; tables: requirements; views: impact and validation.
- Conformance harness, agent eval harness, CI, container image.
- Multilingual semantic search (`find_elements`) with a local embedding model. The former Neo4j pipeline was removed, so there is no database to run.

## Phase 1: Language coverage (the critical path)

Goal: the official examples load. Order the work by what blocks the most files: `tools/conformance.py report` lists the top blockers.

| Step | Constructs | Unblocks |
|---|---|---|
| 1.1 | `standard library package`, `library package`, `alias`, `ref`, `#`/`metadata` annotations, `comment`, `about` | the standard library itself (58 files start with `standard`), and most files' first error |
| 1.2 | Standard library loading: ISQ, SI, ScalarValues and the other model libraries, loaded from the release instead of the built-in stub | units and quantities (`[kg]` resolving to real units) |
| 1.3 | Expressions: arithmetic, comparison, boolean, invocation, `.` and `->` navigation, collection literals | attribute values, `constraint` bodies, `calc` |
| 1.4 | Requirements: `subject`, `require constraint`, `assume`, `actor`, `stakeholder`, `frame concern`, `verify` | requirement semantics and verification |
| 1.5 | Behaviour: `action` bodies, `first`/`then`/succession, `fork`/`join`/`merge`/`decide`, `perform`, `accept`/`send`, `state` bodies, `entry`/`do`/`exit`, `transition`, `exhibit` | activity and state machine views, simulation |
| 1.6 | Interfaces and flows: `bind`, `flow`, `interface` with `end`, `connection` with `end`, `message`, `succession flow` | complete internal block views |
| 1.7 | `individual`, `snapshot`/`timeslice`, `variation`/`variant`, `analysis`/`verification` cases, `view`/`viewpoint`/`render`/`expose` | variability, trades, views |
| 1.8 | Scoping per KerML: visibility, inherited redefinitions, conjugated ports (`~`), feature chaining | resolution without heuristics |

Exit criteria: at least 95% of the release files parse and round-trip, at least 80% resolve with the real standard library, and no `heuristic_reference` on the examples. Keeping `//` comments through a rewrite is also required before the tool is used on real repositories.

## Phase 2: Operations for every concept (what agents and the UI use)

For each construct added in Phase 1, add:
- the operation (or extend `create_element` / `add_relationship`);
- the MCP tool description;
- validation rules;
- eval tasks.

Then add higher-level operations that save a small model many steps:

- `decompose_function(action, into=[...])`: creates sub-actions and their successions.
- `allocate(functions, components)`, plus an allocation matrix query.
- `connect_compatible(a, b)`: proposes or creates compatible ports and the connection.
- `create_requirement(text, satisfied_by=..., verified_by=...)`.
- `check_coverage()`: lists unallocated functions, unsatisfied or unverified requirements, and unconnected ports.
- Configurable model rules (a "model linter"): naming conventions, mandatory `doc`, layering rules. They are written as a project file in the model repository.

Exit criteria: on the eval suite (expanded to at least 60 tasks over every concept), a small model (Haiku) reaches at least 85% success with fewer than 10% tool errors, and a mid-size model reaches at least 95%.

## Phase 3: Views engineers expect

- **Diagrams:**
  - action flow (activity), state machine, sequence, use case, requirements, parametric;
  - the existing structure and definitions views gain editing from the diagram itself (palette, drag and drop).
- **Diagram layout persistence:** a `*.layout.json` file next to each model file, keyed by element ID and updated on rename and move. Layout never goes into the `.sysml` text.
- **Tables and matrices:** generic element tables with filters and inline editing, a traceability matrix (requirement × element), an allocation matrix, and a dependency structure matrix.
- **Views as model elements:** SysML v2 `view`/`viewpoint`/`expose` drive what a diagram shows, so views are versioned with the model.

Exit criteria: an engineer can build the sample vehicle model and a 300-element subsystem from the UI alone, with no text editing. Usability is validated with at least two real users.

## Phase 4: Teamwork and lifecycle

- **Semantic diff and merge:** "part `pump` retyped from `FuelPump` to `Pump2`" rather than text lines. Includes a three-way merge that preserves references and a git merge driver for `.sysml` files.
- **Model review in pull requests:** a CI job posts the semantic diff, the validation errors it introduces, and the impact of the change.
- **Multi-user server mode:** one service per repository, shared by several users, with an edit queue. Today each process syncs through the files.
- **Authentication and authorisation** for the web and HTTP MCP endpoints: SSO via OIDC behind a reverse proxy, and roles for read, edit and admin. Until then, bind to localhost only (the current default).
- **Audit log** of who or what changed what (human or agent, with the tool calls).

## Phase 5: Interoperability and outputs

- **SysML v2 API & Services** (OMG REST API): serve the model so other SysML v2 tools can read and write it, and import from compliant servers.
- **Migration from SysML v1:** import SysML v1 XMI (as exported by desktop tools) into SysML v2 text, mapping blocks, ports, connectors, requirements and allocations. Anything that cannot be mapped is reported. This is the key adoption feature for existing Cameo users; check which SysML v2 exchange formats their tool version supports before designing it.
- **Requirements exchange:** ReqIF import and export, and Excel round-trip.
- **Document generation:** templated system specifications, interface control documents and requirement compliance matrices, in Word, PDF and HTML.
- **Engineering analysis:** solve parametric constraints (mass and power budgets) and run trade studies, with FMI/FMU co-simulation hooks.

## Phase 6: Scale and robustness

- **Incremental indexing and resolution** instead of reindexing the whole workspace on every change. Targets: 50,000 elements with under 200 ms per edit, and under 5 s to load.
- **A language server (LSP)** for `.sysml` in VS Code and other editors, reusing the engine: diagnostics, completion, go-to-definition, rename.
- **Fuzzing** of the parser and the operations (property-based tests: random operation sequences keep the model valid, and undo restores it exactly).

## Working agreement for agents (Claude Code sessions)

- Pick the next item from the current phase. Use `tools/conformance.py report` to order Phase 1 work, and the eval results for Phase 2.
- One PR per item, small and with tests. Run `venv/bin/python -m pytest`, `tools/conformance.py check` and `evals/run.py --oracle` before opening it.
- Lock in conformance gains with `tools/conformance.py check --update-baseline` in the same PR. A PR must never lower the baseline.
- Every new operation gets eval tasks, with a reference solution and checks that fail on an untouched model.
- Update this roadmap's metrics table when a phase milestone moves.
