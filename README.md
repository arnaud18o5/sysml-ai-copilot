# sysml-ai-copilot

An agent-controllable MBSE workbench for **SysML v2** models.

The model is plain SysML v2 textual notation (`.sysml` files under git).
On top of it, the project provides:

- a **model engine**: parser, name resolution, validation and a canonical serializer (text → model → text round trip);
- **deterministic operations** to create, edit, move, delete and link elements, with reference rewriting, atomic batches and undo/redo;
- three front-ends over the *same* operations:
  - an **MCP server** (`mbse-mcp`), so an agent (including a small model) can model through typed tools instead of writing text;
  - a **CLI** (`mbse`) with JSON in and out;
  - a **local web workbench** (`mbse serve`): model browser, internal-structure and definition diagrams, requirements table, impact analysis, validation and editable properties.
- **semantic search** by meaning, in any language (`find_elements`, e.g. « ce qui alimente le moteur »), with a local embedding model and no API key.

The design goal: a large model *could* edit SysML text directly, but a small one needs guard rails. Every operation either applies exactly what was asked and reports what changed, or refuses with a structured error (`code`, `message`, `suggestions`) and leaves the model untouched.

## Quick start

```bash
python3 -m venv venv
venv/bin/pip install -e ".[mcp,semantic,dev]"

venv/bin/mbse -m data/ check              # validate (non-zero exit on errors, CI friendly)
venv/bin/mbse -m data/ serve              # web workbench on http://127.0.0.1:8765
```

### Use it from an agent (MCP)

The repo ships a `.mcp.json`, so Claude Code picks up the `sysml-mbse` server for `data/` automatically. For other clients:

```json
{
  "mcpServers": {
    "sysml-mbse": { "command": "/path/to/venv/bin/mbse-mcp", "args": ["/path/to/models"] }
  }
}
```

Changes are saved to disk after each successful operation (`--no-autosave` keeps them in memory until `save`).

Tools:

| Read | Write | Control |
|---|---|---|
| `summary`, `get_tree`, `find_elements`, `list_elements`, `get_element`, `list_relationships`, `impact_analysis`, `requirements_trace`, `validate`, `get_source`, `list_kinds` | `create_element`, `update_element`, `delete_element`, `move_element`, `add_relationship`, `remove_relationship`, `add_import`, `remove_import`, `add_from_text`, `create_document`, `apply_batch` | `undo`, `redo`, `diff`, `save` |

### Use it from the shell

```bash
mbse -m data/ get_element element=VehicleFuelSystem.Vehicle
mbse -m data/ create_element kind="part def" name=Filter owner=VehicleFuelSystem doc="Removes contaminants"
mbse -m data/ add_relationship kind=connect source=VehicleFuelSystem.Vehicle.tank.fuelOut target=VehicleFuelSystem.Vehicle.engine.fuelIn
mbse -m data/ --dry-run update_element element=VehicleFuelSystem.FuelTank name="Fuel Tank"   # prints the diff only
mbse -m data/ impact_analysis element=VehicleFuelSystem.FuelPump
mbse -m data/ fmt                         # rewrite files in canonical form
mbse ops                                  # list operations
```

Arguments are `key=value` pairs (values parsed as JSON when possible) or a single JSON object.

## Concepts

**Element IDs** are qualified names: `VehicleFuelSystem.Vehicle.engine`. Names that aren't plain identifiers are quoted: `Pkg.'Fuel tank'`. A **feature chain** through a usage's type is also a valid ID, e.g. `VehicleFuelSystem.Vehicle.tank.fuelOut` is the `fuelOut` port of the `tank` part (declared on `FuelTank`). Connections use chains.

**Name resolution** follows a pragmatic subset of SysML v2 scoping: the enclosing namespaces outward, then the root namespace, then the built-in `ScalarValues` library (`Real`, `Integer`, `Boolean`, `String`...). Looking a name up *inside* an element also checks its imports, and the features it inherits from its types (`:`) and supertypes (`:>`). A reference that only resolves by a model-wide name search is accepted with a `heuristic_reference` warning that suggests the proper qualified form.

**Guarantees of write operations**

- Renaming or moving an element rewrites every reference to it (and from it), including feature chains such as `tank.fuelOut`.
- Deleting a referenced element is refused, with the list of references, unless `cascade=true`, which also removes them.
- Typing is checked (a `port` must be typed by a `port def`, etc.).
- An inherited feature can't be edited through a chain. The error names the element that declares it.
- `apply_batch` is all-or-nothing. `add_from_text` is rejected if it introduces validation errors.
- Every change can be undone, and `diff` shows unsaved changes.

**Search**: `find_elements` combines a lexical match on names (camelCase aware) and doc words with a semantic match. The semantic match embeds each element's label (name, kind, type) and its doc with a local multilingual model (fastembed, `paraphrase-multilingual-MiniLM-L12-v2`, about 220 MB, downloaded from Hugging Face on first use).
- An exact name always ranks first.
- `mode` can force `lexical` or `semantic`.
- Without the `semantic` extra, or without access to the model, `auto` mode falls back to lexical and says so.

Settings: `MBSE_SEMANTIC=off` (auto mode stays lexical), `MBSE_EMBEDDING_MODEL`, `MBSE_EMBEDDING_CACHE_DIR`, `MBSE_MIN_SEMANTIC_SCORE`. The web search box runs a semantic search on Enter.

**Derived relations**: `CONTAINS`, `TYPED_BY`, `SPECIALIZES` (definitions), `SUBSETS` / `REDEFINES` (usages), `CONNECTS_TO`, `SATISFIES`, `ALLOCATES`. `impact_analysis` follows all but `CONTAINS`. By default it follows them in the *dependents* direction ("what is affected if X changes") and explains every result with its relation chain.

## Supported SysML v2 subset

```sysml
package VehicleFuelSystem {
    private import ScalarValues::*;

    abstract part def Base;
    part def Vehicle :> Base {
        doc /* The whole vehicle. */
        part engine : Engine;
        part wheels : Wheel[4];
        in port ctrl : Ctrl;
        attribute mass : Real = 1200 [kg];
        connect tank.fuelOut to engine.fuelIn;
    }
    part def Light :> Vehicle {
        attribute :>> mass = 900;
    }
    requirement def R1 { doc /* ... */ }
    satisfy R1 by Vehicle.engine;
    allocate Drive to Vehicle;
}
```

- **Definitions and usages** for `part`, `port`, `attribute`, `item`, `action`, `state`, `requirement`, `constraint`, `interface`, `connection`, `allocation`, `enum`, `occurrence`, `calc`, `use case`, `verification case`, `analysis case`, `view`, `viewpoint` and `concern`.
- **Modifiers and features:** `abstract`, directions (`in`/`out`/`inout`), multiplicities, typing (`:`), specialization and subsetting (`:>`), redefinition (`:>>`) and `doc`.
- **Values:** numbers with an optional unit, strings, booleans and references.
- **Statements:** `connect`, `satisfy` and `allocate`.
- **Packages and imports:** nested packages, several packages per file, multi-file workspaces, and `import` with `*` / `**` wildcards.

Not supported yet: expressions and constraints bodies, behaviour (succession, transitions, `perform`/`exhibit`), `bind`, `flow`, `ref`, metadata. Plain `//` and `/* */` comments are dropped when a file is rewritten; `doc` comments are kept.

## Project structure

```
sysml_copilot/
  model.py        Metamodel tree (Element, Relationship, Import, Ref, Value)
  syntax.py       Lark grammar, parser, canonical serializer
  workspace.py    Multi-file workspace: IDs, name resolution, derived relations, validation
  operations.py   Deterministic operations (ModelService) + `call()` dispatcher
  mcp_server.py   MCP server (mbse-mcp)
  cli.py          Command line (mbse)
  web/            Local web workbench (mbse serve)
  semantic.py     Semantic search: element descriptions, local embeddings, cache
  config.py       Embedding model and search settings (env overridable)
tests/            pytest suite (parser, resolution, operations, CLI, MCP, web API, evals)
tools/conformance.py   OMG SysML v2 release conformance harness (+ baseline)
evals/            Agent eval harness: tasks, starting models, runner
docs/ROADMAP.md   Phased plan with measurable exit criteria
data/sample.sysml Example model (vehicle fuel system)
```

## Deployment

```bash
docker compose up -d --build                          # web on :8765, MCP (HTTP) on :8766/mcp
MODELS_DIR=/path/to/model-repo docker compose up -d
```

The web workbench and the MCP server are separate processes working on the same files. Each one picks up changes made by the other, or by a `git pull`. A session with unsaved changes refuses writes with a `conflict` error instead of overwriting them. The UI refreshes live, so you can watch an agent build the model.

There is no authentication yet: ports are bound to `127.0.0.1`. Put a reverse proxy with SSO in front before exposing them (see `docs/ROADMAP.md`, Phase 4).

## Measuring progress

- **Conformance with the official OMG SysML v2 release** (examples, training, validation, standard library), at parse, round-trip and resolve levels. CI fails on any regression against `tools/conformance_baseline.json`.
  ```bash
  venv/bin/python tools/conformance.py fetch && venv/bin/python tools/conformance.py report
  ```
- **Agent evals.** `evals/tasks.json` holds modelling tasks: queries, creation, relationships, refactoring, fixing a broken model, and multi-step design. Each one is graded by deterministic checks on the resulting model.
  ```bash
  venv/bin/python evals/run.py --oracle                     # reference solutions (CI)
  venv/bin/python evals/run.py --model claude-haiku-5-5     # a small model through the MCP tools
  ```

The plan towards an enterprise-ready tool, with exit criteria per phase, is in [docs/ROADMAP.md](docs/ROADMAP.md).

## Development

```bash
venv/bin/python -m pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch, commit and PR workflow.
