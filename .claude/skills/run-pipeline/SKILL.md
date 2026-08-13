---
name: run-pipeline
description: Start Neo4j, ingest a SysML v2 file, and run a natural-language impact-analysis query against it. Use whenever the user wants to test a change to the parser/ingestion/query code, or explore a SysML v2 model.
---

# Run the ingestion + query pipeline

1. Make sure Neo4j is running:
   ```bash
   cd <repo-root>
   docker compose up -d
   ```
   Wait for it to be ready if it was just started:
   ```bash
   for i in $(seq 1 30); do docker logs sysml-copilot-neo4j 2>&1 | grep -q "Started." && break; sleep 2; done
   ```

2. Always use the venv interpreter explicitly — `python`/`pip` on this machine are aliased to a non-venv interpreter:
   ```bash
   venv/bin/python -m sysml_copilot.ingest data/sample.sysml
   ```
   Replace `data/sample.sysml` with whatever file is being tested. Ingestion wipes and reloads the whole graph (`reset_graph` in `ingest.py`) — it's not incremental, so re-running it is always safe/expected when iterating.

3. Run a query:
   ```bash
   venv/bin/python -m sysml_copilot.query "your question here"
   ```
   The output shows the resolved element (plus runner-up candidates and their similarity scores) and the impact-analysis traversal result with hop distances. Check the score of the resolved element, not just whether *an* element was returned — a low top score (below ~0.7-0.8) usually means the resolution picked the wrong element, especially for short/generic element names.

4. If you need to inspect the graph directly, use the Neo4j browser at `http://localhost:7474` (credentials in `docker-compose.yml`), or run Cypher via the driver in a one-off script.
