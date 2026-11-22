---
name: roadmap-work
description: Pick and deliver the next roadmap item for the MBSE workbench (language coverage, operations, views, interop), measured by the conformance and agent-eval harnesses. Use when the user asks to continue the roadmap, improve SysML v2 coverage, make small models better at the tools, or "work on the next thing".
---

# Working the roadmap

The plan and its exit criteria are in `docs/ROADMAP.md`. Progress is measured, not asserted.

## 1. Measure where we are

```bash
venv/bin/pip install -e ".[mcp,dev,eval]"           # once
venv/bin/python tools/conformance.py fetch          # once: pinned OMG SysML v2 release into .cache/
venv/bin/python tools/conformance.py report         # parse / roundtrip / resolve rates + top blockers
venv/bin/python evals/run.py --oracle               # eval suite sanity (no API)
venv/bin/python evals/run.py --model claude-haiku-5-5   # small-model success rate (needs Anthropic credentials)
```

## 2. Pick one item

- **Phase 1 (language):** take the top blocker from `conformance.py report`. Follow the `extend-grammar` skill.
- **Phase 2 (operations):** take the failing eval tasks or the missing concepts. A failure where the model called the wrong tool or passed bad arguments usually means the tool description or the error message needs fixing. It is not the model's fault.
- **Later phases:** take the next unchecked bullet in `docs/ROADMAP.md`.

Keep the PR small: one construct, one operation, one view.

## 3. Deliver with evidence

- Tests in `tests/` for the new behaviour. The whole suite stays green (`venv/bin/python -m pytest`).
- `tools/conformance.py check` shows no regression. Lock in gains with `--update-baseline` in the same PR.
- A new or changed operation means new tasks in `evals/tasks.json`, each with a `reference` solution and checks that fail on an untouched model. `tests/test_evals.py` enforces both.
- Update the metrics table in `docs/ROADMAP.md` when the numbers move.
- Write the PR description with the before and after numbers (conformance percentages, eval success).
