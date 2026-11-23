from pathlib import Path
from threading import Barrier

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from workflow.audit import Audit
from workflow.graph import build_graph
from workflow.provider import AMBIGUOUS_REQUIREMENT, AMBIGUOUS_SCOPE, Edit, MockProvider
from workflow.validation import PolicyError, RunnerUnavailable, check_edits
from workflow.workspace import Workspace, git

ROOT = Path(__file__).resolve().parents[1]


class Run:
    def __init__(self, tmp_path, scenario, provider=None, validator=None):
        self.workspace = Workspace(tmp_path)
        self.path = tmp_path / "workflow.sqlite"
        self.audit = Audit(self.path)
        self.baseline = self.workspace.initialize(scenario, ROOT)
        self.provider = provider or MockProvider(ROOT)
        self.validator = validator or (lambda _tree, _scenario: (True, "Simulated pass"))
        self.config = {"configurable": {"thread_id": "demo"}}
        self.input = {
            "run": "demo", "scenario": scenario, "revision": 1, "attempt": 0,
            "requirement": AMBIGUOUS_REQUIREMENT if scenario == "ambiguous" else "Build the shortener",
            "clarification": "", "baseline": self.baseline, "candidate": "", "validation": "",
        }

    def invoke(self, value):
        # Reopen the saver and reconstruct the graph at every CLI boundary.
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            graph = build_graph(self.provider, self.workspace, self.audit, self.validator, saver)
            if value is not False:
                graph.invoke(value, self.config, durability="sync")
            return graph.get_state(self.config)

    def approval(self, snapshot, **overrides):
        gate = snapshot.interrupts[0].value
        return Command(resume={**gate, "decision": "approve", "actor": "reviewer", **overrides})

    def review(self):
        return git(self.workspace.repo, "branch", "--list", "review")


def test_parallel_join_and_promotion_after_restart(tmp_path):
    barrier = Barrier(2, timeout=5)

    class ParallelMock(MockProvider):
        def generate(self, role, schema, context):
            if role in {"architecture", "tests"}:
                barrier.wait()
            if role == "implement":
                assert context["architecture"]["decisions"]
                assert context["tests"]["decisions"]
            return super().generate(role, schema, context)

    run = Run(tmp_path, "greenfield", provider=ParallelMock(ROOT))
    pending = run.invoke(run.input)
    assert pending.interrupts[0].value["kind"] == "approval"
    assert run.review() == ""
    persisted = run.invoke(False)
    result = run.invoke(run.approval(persisted))
    assert result.values["status"] == "approved"
    assert git(run.workspace.repo, "rev-parse", "review") == pending.values["candidate"]
    assert git(run.workspace.repo, "rev-parse", "main") == run.baseline
    assert run.audit.metrics()["success_rate"] == 1


@pytest.mark.parametrize("decision", ["reject", "approve"])
def test_rejection_and_stale_commit_restore_baseline(tmp_path, decision):
    run = Run(tmp_path, "brownfield")
    assert "expires_at" not in run.workspace.files()["shortener/app.py"]
    pending = run.invoke(run.input)
    assert "expires_at" in run.workspace.files()["shortener/app.py"]
    result = run.invoke(run.approval(pending, decision=decision, commit=run.baseline))
    assert result.values["status"] == ("rejected" if decision == "reject" else "stopped")
    assert git(run.workspace.tree, "rev-parse", "HEAD") == run.baseline
    assert run.review() == ""
    assert run.audit.metrics()["rollbacks"] == 1


@pytest.mark.parametrize("recover", [True, False])
def test_repair_and_safe_stop(tmp_path, recover):
    calls = []

    def validate(_tree, _scenario):
        calls.append(True)
        return recover and len(calls) == 2, "simulated validation result"

    run = Run(tmp_path, "greenfield", validator=validate)
    state = run.invoke(run.input)
    assert len(calls) == (2 if recover else 3)
    assert run.audit.metrics()["retries"] == len(calls) - 1
    if recover:
        assert state.interrupts[0].value["kind"] == "approval"
        assert run.audit.metrics()["mean_recovery_seconds"] is not None
    else:
        assert state.values["status"] == "stopped"
        assert git(run.workspace.tree, "rev-parse", "HEAD") == run.baseline


def test_unavailable_runner_stops_without_approval(tmp_path):
    def unavailable(_tree, _scenario):
        raise RunnerUnavailable("offline")

    run = Run(tmp_path, "greenfield", validator=unavailable)
    result = run.invoke(run.input)
    assert result.values["status"] == "stopped"
    assert not result.interrupts
    assert result.values["attempt"] == 1
    assert run.review() == ""


def test_ambiguity_and_replanning(tmp_path):
    run = Run(tmp_path, "ambiguous")
    pending = run.invoke(run.input)
    assert pending.interrupts[0].value["kind"] == "clarification"
    assert not any(event["kind"] == "design" for event in run.audit.read())
    pending = run.invoke(Command(resume=AMBIGUOUS_SCOPE))
    assert pending.values["revision"] == 2
    assert pending.values["analysis"]["intent"] == AMBIGUOUS_SCOPE
    new = run.invoke(run.approval(pending, decision="revise", requirement=AMBIGUOUS_SCOPE))
    assert new.values["revision"] == 3
    assert new.interrupts[0].value["kind"] == "approval"
    result = run.invoke(run.approval(new))
    assert result.values["status"] == "approved"
    assert [event["details"]["revision"] for event in run.audit.read() if event["kind"] == "join"] == [2, 3]


def test_worktree_recovers_from_local_drift_before_promotion(tmp_path):
    run = Run(tmp_path, "greenfield")
    state = run.invoke(run.input)
    (run.workspace.tree / "shortener/app.py").write_text("unreviewed local modification")
    result = run.invoke(run.approval(state))
    assert result.values["status"] == "approved"
    source = git(run.workspace.repo, "show", "review:shortener/app.py")
    assert source == (ROOT / "shortener/app.py").read_text().strip()


def test_policy_checks_before_writing(tmp_path):
    with pytest.raises(PolicyError):
        check_edits([Edit(path="shortener/app.py", content="import subprocess")], tmp_path)
    (tmp_path / "shortener").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PolicyError):
        check_edits([Edit(path="shortener/app.py", content="x = 1")], tmp_path)


def test_resume_interrupted_validation(tmp_path):
    calls = []

    def validate(_tree, _scenario):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("simulated interrupted worker")
        return True, "Simulated pass after restart"

    run = Run(tmp_path, "greenfield", validator=validate)
    with pytest.raises(RuntimeError, match="interrupted worker"):
        run.invoke(run.input)
    before = run.invoke(False)
    assert before.next == ("validate",)
    assert before.values["attempt"] == 1
    assert run.review() == ""
    after = run.invoke(None)
    assert after.interrupts[0].value["kind"] == "approval"
    assert after.values["candidate"] == before.values["candidate"]
    assert after.values["attempt"] == 1
