"""Agent evaluation: can a (small) model do MBSE work through the mbse tools?

Each task in `evals/tasks.json` starts from a model file, gives the agent a
natural-language instruction, and grades the final model state (and, for
questions, the final answer) with deterministic checks. Nothing is graded by
an LLM.

    venv/bin/python evals/run.py --oracle                  # apply reference solutions (no API, CI)
    venv/bin/python evals/run.py --model claude-haiku-5-5  # run an agent (needs Anthropic credentials)
    venv/bin/python evals/run.py --model claude-haiku-5-5 --tasks m- --repeat 3

The agent sees exactly the MCP server's tools (same names, schemas,
descriptions and instructions), executed in-process on a scratch copy of the
task's model. Results are written to evals/results/ (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sysml_copilot.operations import ModelService, call  # noqa: E402
from sysml_copilot.syntax import serialize_document  # noqa: E402

TASKS = os.path.join(ROOT, "evals", "tasks.json")
RESULTS = os.path.join(ROOT, "evals", "results")
DEFAULT_MODEL = "claude-haiku-5-5"

SYSTEM_SUFFIX = """
You are an MBSE assistant. Do the user's request using the tools; never invent ids.
When you are done, reply with a short final answer (in the user's language) and stop
calling tools."""


# ----------------------------------------------------------------- checks

@dataclass
class CheckResult:
    ok: bool
    check: dict
    detail: str = ""


def _types_of(svc, eid):
    el = svc.get_element(eid, include_source=False)
    return [t["id"] for t in el.get("types", [])]


def _relation_present(svc, rtype, source, target, undirected=False):
    rels = svc.list_relationships(type=rtype)["relations"]

    def same(item, key, wanted):
        return wanted in (item.get(key), item.get(f"{key}_chain"))

    for r in rels:
        if same(r, "source", source) and same(r, "target", target):
            return True
        if undirected and same(r, "source", target) and same(r, "target", source):
            return True
    return False


def run_checks(task, svc: ModelService, answer: str, initial_text: dict) -> list[CheckResult]:
    out = []
    for chk in task["checks"]:
        try:
            out.append(_run_check(chk, svc, answer or "", initial_text))
        except Exception as exc:  # a check must never crash the run
            out.append(CheckResult(False, chk, f"check error: {type(exc).__name__}: {exc}"))
    return out


def _run_check(chk, svc, answer, initial_text) -> CheckResult:
    if "exists" in chk:
        eid = chk["exists"]
        if svc.ws.get(eid) is None:
            return CheckResult(False, chk, f"{eid} does not exist")
        el = svc.get_element(eid, include_source=False)
        if "kind" in chk and el["kind"] != chk["kind"]:
            return CheckResult(False, chk, f"kind is {el['kind']}")
        if "type" in chk and chk["type"] not in _types_of(svc, eid):
            return CheckResult(False, chk, f"types are {_types_of(svc, eid)}")
        if "direction" in chk and el.get("direction") != chk["direction"]:
            return CheckResult(False, chk, f"direction is {el.get('direction')}")
        if "doc_contains" in chk and chk["doc_contains"].lower() not in (el.get("doc") or "").lower():
            return CheckResult(False, chk, f"doc is {el.get('doc')!r}")
        if "value" in chk and (el.get("value") or {}).get("value") != chk["value"]:
            return CheckResult(False, chk, f"value is {el.get('value')}")
        return CheckResult(True, chk)
    if "absent" in chk:
        ok = svc.ws.get(chk["absent"]) is None
        return CheckResult(ok, chk, "" if ok else "still exists")
    if "relation" in chk:
        present = _relation_present(svc, chk["relation"], chk["source"], chk["target"], chk.get("undirected", False))
        wanted = chk.get("present", True)
        return CheckResult(present == wanted, chk, "" if present == wanted else
                           ("missing" if wanted else "should have been removed"))
    if "valid" in chk:
        v = svc.validate()
        ok = v["errors"] == 0 and (not chk.get("no_warnings") or v["warnings"] == 0)
        msgs = [d["message"] for d in v["diagnostics"] if d["severity"] in ("error", "warning")][:3]
        return CheckResult(ok, chk, "" if ok else "; ".join(msgs))
    if "source_contains" in chk:
        text = "".join(serialize_document(d) for d in svc.ws.documents)
        ok = chk["source_contains"] in text
        return CheckResult(ok, chk, "" if ok else "text not found")
    if "unchanged" in chk:
        now = {d.path: serialize_document(d) for d in svc.ws.documents}
        ok = now == initial_text
        return CheckResult(ok, chk, "" if ok else "the model was modified")
    if "answer_contains" in chk:
        missing = [w for w in chk["answer_contains"] if w.lower() not in answer.lower()]
        return CheckResult(not missing, chk, f"missing {missing}" if missing else "")
    if "answer_contains_any" in chk:
        ok = any(w.lower() in answer.lower() for w in chk["answer_contains_any"])
        return CheckResult(ok, chk, "" if ok else f"none of {chk['answer_contains_any']}")
    return CheckResult(False, chk, "unknown check")


# ------------------------------------------------------------------ setup

def load_tasks(filter_=None):
    with open(TASKS, encoding="utf-8") as f:
        tasks = json.load(f)["tasks"]
    if filter_:
        tasks = [t for t in tasks if any(t["id"].startswith(p) or t["id"] == p for p in filter_.split(","))]
    return tasks


def fresh_service(task, workdir):
    src = os.path.join(ROOT, task["model"])
    dst = os.path.join(workdir, os.path.basename(src))
    shutil.copy(src, dst)
    svc = ModelService([dst])
    return svc, {d.path: serialize_document(d) for d in svc.ws.documents}


def tool_definitions():
    """The MCP server's tools, converted to Messages API tool definitions."""
    from sysml_copilot.mcp_server import INSTRUCTIONS, build_server

    server = build_server(ModelService())
    tools = asyncio.run(server.list_tools())
    defs = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in tools if t.name not in ("save", "diff")]
    return defs, INSTRUCTIONS


# ----------------------------------------------------------------- oracle

def run_oracle(tasks):
    failures = 0
    for task in tasks:
        with tempfile.TemporaryDirectory() as wd:
            svc, initial = fresh_service(task, wd)
            errors = []
            for step in task.get("reference", []):
                res = call(svc, step["op"], step["args"])
                if not res.get("ok") and not step.get("optional"):
                    errors.append(f"{step['op']}: {res['error']['message']}")
            answer = task.get("reference_answer", "")
            checks = run_checks(task, svc, answer, initial)
            ok = not errors and all(c.ok for c in checks)
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {task['id']}")
            for e in errors:
                print(f"      reference step failed: {e}")
            for c in checks:
                if not c.ok:
                    print(f"      check failed: {json.dumps(c.check)} -> {c.detail}")
    print(f"\n{len(tasks) - failures}/{len(tasks)} tasks solvable by their reference solution")
    return 1 if failures else 0


# ------------------------------------------------------------------ agent

@dataclass
class Run:
    task: str
    success: bool = False
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    stop: str = ""
    answer: str = ""
    failed_checks: list = field(default_factory=list)
    transcript: list = field(default_factory=list)


def run_agent(task, model, effort, max_turns, tools, instructions) -> Run:
    import anthropic

    client = anthropic.Anthropic()
    run = Run(task["id"])
    start = time.time()
    with tempfile.TemporaryDirectory() as wd:
        svc, initial = fresh_service(task, wd)
        messages = [{"role": "user", "content": task["prompt"]}]
        kwargs = {"output_config": {"effort": effort}} if effort else {}
        response = None
        while run.turns < max_turns:
            run.turns += 1
            try:
                response = client.messages.create(
                    model=model, max_tokens=16000, system=instructions + SYSTEM_SUFFIX,
                    tools=tools, messages=messages, **kwargs)
            except anthropic.APIStatusError as exc:
                run.stop = f"api_error {exc.status_code}: {exc.message}"
                break
            run.input_tokens += response.usage.input_tokens
            run.output_tokens += response.usage.output_tokens
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                run.stop = response.stop_reason
                break
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                run.tool_calls += 1
                args = {k: v for k, v in (block.input or {}).items() if v is not None}
                res = call(svc, block.name, args)
                if not res.get("ok"):
                    run.tool_errors += 1
                run.transcript.append({"tool": block.name, "args": args, "ok": res.get("ok")})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(res, ensure_ascii=False)[:20000],
                                **({} if res.get("ok") else {"is_error": True})})
            messages.append({"role": "user", "content": results})
        else:
            run.stop = "max_turns"
        if response is not None:
            run.answer = "\n".join(b.text for b in response.content if b.type == "text")
        checks = run_checks(task, svc, run.answer, initial)
        run.failed_checks = [{"check": c.check, "detail": c.detail} for c in checks if not c.ok]
        run.success = not run.failed_checks and not run.stop.startswith("api_error")
    run.seconds = round(time.time() - start, 1)
    return run


def run_agents(tasks, model, effort, max_turns, repeat):
    tools, instructions = tool_definitions()
    runs = []
    for task in tasks:
        for i in range(repeat):
            r = run_agent(task, model, effort, max_turns, tools, instructions)
            runs.append(r)
            status = "PASS" if r.success else "FAIL"
            print(f"{status}  {task['id']}#{i + 1}  turns={r.turns} calls={r.tool_calls} "
                  f"errors={r.tool_errors} tokens={r.input_tokens}+{r.output_tokens} {r.seconds}s  stop={r.stop}")
            for fc in r.failed_checks:
                print(f"      {json.dumps(fc['check'])} -> {fc['detail']}")
    passed = sum(r.success for r in runs)
    by_cat = {}
    for task in tasks:
        rs = [r for r in runs if r.task == task["id"]]
        c = by_cat.setdefault(task["category"], [0, 0])
        c[0] += sum(r.success for r in rs)
        c[1] += len(rs)
    print(f"\n{model}: {passed}/{len(runs)} runs passed ({100 * passed / max(1, len(runs)):.0f}%)")
    for cat, (p, n) in sorted(by_cat.items()):
        print(f"  {cat:<14} {p}/{n}")
    print(f"  avg tool calls {sum(r.tool_calls for r in runs) / max(1, len(runs)):.1f}, "
          f"tool error rate {sum(r.tool_errors for r in runs) / max(1, sum(r.tool_calls for r in runs)):.0%}, "
          f"tokens in/out {sum(r.input_tokens for r in runs)}/{sum(r.output_tokens for r in runs)}")
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{time.strftime('%Y%m%d-%H%M%S')}-{model}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"model": model, "effort": effort, "passed": passed, "runs": len(runs),
                   "results": [r.__dict__ for r in runs]}, f, indent=1, ensure_ascii=False)
    print(f"details: {path}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oracle", action="store_true", help="apply reference solutions instead of an agent")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    ap.add_argument("--tasks", help="comma-separated task ids or id prefixes")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-turns", type=int, default=30)
    args = ap.parse_args(argv)
    tasks = load_tasks(args.tasks)
    if not tasks:
        raise SystemExit("no task matches")
    if args.oracle:
        return run_oracle(tasks)
    return run_agents(tasks, args.model, args.effort, args.max_turns, args.repeat)


if __name__ == "__main__":
    sys.exit(main())
