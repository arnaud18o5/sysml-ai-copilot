# sysml-ai-copilot

An AI copilot prototype for analyzing SysML v2 models: parse the textual notation, load it into a graph database, and answer natural-language questions like *"what is the impact of element X?"*.

## Architecture

- **Parser** — a Lark grammar for a subset of the SysML v2 textual notation (`package`, `part def`, `port def`, part/port usages, `connect`, `requirement def`, `satisfy`). It extracts typed elements and relations, resolving references (including through feature typing, e.g. `tank.fuelOut` where `tank` is typed by `FuelTank`).
- **Graph store (Neo4j)** — elements become `:Element` nodes; relations become typed edges (`CONTAINS`, `TYPED_BY`, `CONNECTS_TO`, `SATISFIES`). Graph traversal answers structural questions like impact analysis.
- **Vector search (Neo4j native vector index)** — each element gets an embedding (name, kind, doc) via [fastembed](https://github.com/qdrant/fastembed) (local, no API key required). Natural-language queries are embedded and matched against this index to resolve "which element is the user talking about" before the graph traversal runs.

This hybrid approach (vector search to resolve NL → element, graph traversal to reason over relations) is similar to GraphRAG.

## Prerequisites

- Python 3.12+
- Docker (for Neo4j)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

docker compose up -d
```

## Usage

Ingest a SysML v2 model:

```bash
python -m sysml_copilot.ingest data/sample.sysml
```

Run an impact-analysis query in natural language:

```bash
python -m sysml_copilot.query "what is the impact of the fuel pump?"
```

Optional flags (must precede the question), `--top-k` (candidates returned by vector search, default 3) and `--max-hops` (impact-analysis traversal depth, default 3):

```bash
python -m sysml_copilot.query --top-k 5 --max-hops 2 "what is the impact of the fuel pump?"
```

Neo4j browser is available at `http://localhost:7474` (default credentials: `neo4j` / `password123`, see `docker-compose.yml`).

## Project structure

```
sysml_copilot/
  parser.py      SysML v2 textual notation parser (subset)
  ingest.py       Loads parsed elements/relations into Neo4j + builds embeddings
  embeddings.py   Local embedding generation via fastembed
  query.py        NL resolution (vector search) + impact analysis (graph traversal)
  config.py       Neo4j connection settings, embedding model config
data/
  sample.sysml    Small example model (vehicle fuel system) for testing
```

## Known limitations

- Only a subset of SysML v2 textual notation is supported — no imports, no feature specialization/redefinition, no typed attributes yet.
- Reference resolution is a simplified heuristic, not a full implementation of the SysML v2 metamodel's scoping rules.
- Impact analysis traverses `CONNECTS_TO`, `TYPED_BY`, and `SATISFIES` edges but intentionally excludes `CONTAINS`, to avoid pulling in unrelated siblings under the same package — this means nested features of the queried element (e.g. its ports) aren't reached unless they're queried directly.
- Retrieval quality depends heavily on the richness of element `doc` text and on the embedding model matching the query language.
- Model modification (write-back) is not implemented yet — this prototype only covers ingestion and read-only analysis.
