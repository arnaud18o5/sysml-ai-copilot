"""The eval suite itself must be sound: every task solvable by its reference
solution, no task passing on an untouched model, and the agent loop working
end to end (with a scripted fake client, so no API call is made)."""

import importlib.util
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
spec = importlib.util.spec_from_file_location("evals_run", os.path.join(ROOT, "evals", "run.py"))
ev = importlib.util.module_from_spec(spec)
sys.modules["evals_run"] = ev
spec.loader.exec_module(ev)


def test_reference_solutions_pass(capsys):
    assert ev.run_oracle(ev.load_tasks()) == 0


@pytest.mark.parametrize("task", ev.load_tasks(), ids=lambda t: t["id"])
def test_untouched_model_fails(task):
    with tempfile.TemporaryDirectory() as wd:
        svc, initial = ev.fresh_service(task, wd)
        checks = ev.run_checks(task, svc, "", initial)
    assert not all(c.ok for c in checks), "a task must not pass without doing anything"


class FakeClient:
    """Replays a task's reference solution as tool_use blocks, then answers."""

    def __init__(self, steps, answer):
        self.steps, self.answer, self.i = list(steps), answer, 0
        self.messages = self

    def create(self, **kwargs):
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        if self.i < len(self.steps):
            step = self.steps[self.i]
            self.i += 1
            block = SimpleNamespace(type="tool_use", id=f"t{self.i}", name=step["op"], input=step["args"])
            return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
        text = SimpleNamespace(type="text", text=self.answer)
        return SimpleNamespace(content=[text], stop_reason="end_turn", usage=usage)


@pytest.mark.parametrize("task_id", ["m-insert-filter", "q-count-requirements", "f-fix-broken"])
def test_agent_loop_with_fake_client(monkeypatch, task_id):
    pytest.importorskip("anthropic")
    pytest.importorskip("mcp")
    import anthropic

    task = next(t for t in ev.load_tasks() if t["id"] == task_id)
    fake = FakeClient(task.get("reference", []), task.get("reference_answer", "done"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: fake)
    tools, instructions = ev.tool_definitions()
    assert {t["name"] for t in tools} >= {"create_element", "add_relationship", "validate"}
    run = ev.run_agent(task, "fake-model", None, 30, tools, instructions)
    assert run.success, run.failed_checks
    assert run.tool_calls == len(task.get("reference", []))
    assert run.stop == "end_turn"
