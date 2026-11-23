"""Explicit SDLC graph with durable human gates and bounded repair."""

import json
import subprocess
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from workflow.provider import Analysis, Design, Implementation
from workflow.validation import PolicyError, RunnerUnavailable
from workflow.workspace import git

MAX_ATTEMPTS = 3


class State(TypedDict, total=False):
    run: str
    scenario: str
    revision: int
    requirement: str
    clarification: str
    baseline: str
    attempt: int
    analysis: dict
    architecture: dict
    tests: dict
    implementation: dict
    candidate: str
    validation: str
    error: str
    status: str
    decision: str


def build_graph(provider, workspace, audit, validate, checkpointer):
    graph = StateGraph(State)
    report = workspace.tree.parent / "review.md"

    def event(state, key, kind, **data):
        audit.record(state["run"], f'{state["revision"]}:{key}', kind,
                     revision=state["revision"], **data)

    def ask(state, role, schema, context):
        for attempt in range(2):
            try:
                return provider.generate(role, schema, context).model_dump()
            except (httpx.HTTPError, ValidationError, KeyError, IndexError, ValueError) as exc:
                event(state, f'{role}:{state["attempt"]}:{attempt}:failure', "failure",
                      stage=role, reason=type(exc).__name__)
                if not attempt:
                    event(state, f'{role}:{state["attempt"]}:retry', "retry", stage=role)
        return {"error": f"{role} provider failed twice"}

    def analyze(state):
        workspace.reset(state["baseline"])
        context = {key: state[key] for key in ("scenario", "requirement", "clarification")}
        context["files"] = workspace.files()
        value = ask(state, "analyze", Analysis, context)
        seen = set()
        for task in value.get("tasks", []):
            if not task["id"] or task["id"] in seen or set(task["depends_on"]) - seen:
                value = {"error": "Plan contains invalid dependencies"}
                break
            seen.add(task["id"])
        if not seen and not value.get("question") and not value.get("error"):
            value = {"error": "Plan has no tasks"}
        event(state, "analysis", "analysis", requirement=state["requirement"],
              clarification=state["clarification"], baseline=state["baseline"], output=value)
        return {"analysis": value, "error": value.get("error", ""), "status": "analyzed"}

    def after_analysis(state):
        if state["error"]:
            return "stop"
        if state["analysis"]["question"]:
            return "clarify"
        return ["architecture", "tests"]

    def revised(state, clarification="", requirement=None):
        return {
            "revision": state["revision"] + 1, "attempt": 0,
            "requirement": requirement if requirement is not None else state["requirement"],
            "clarification": clarification, "analysis": {}, "architecture": {}, "tests": {},
            "implementation": {}, "validation": "", "candidate": "", "error": "",
            "decision": "", "status": "replanning",
        }

    def clarify(state):
        answer = interrupt({"kind": "clarification", "question": state["analysis"]["question"]})
        event(state, "clarification", "clarification", answer=answer)
        return revised(state, clarification=answer)

    def design(role):
        def node(state):
            value = ask(state, role, Design, {"analysis": state["analysis"], "files": workspace.files()})
            event(state, role, "design", stage=role, output=value)
            return {role: value}
        return node

    def join(state):
        error = state["architecture"].get("error") or state["tests"].get("error") or ""
        event(state, "join", "join", passed=not error)
        return {"error": error}

    def implement(state):
        attempt = state["attempt"] + 1
        start = state["candidate"] or state["baseline"]
        workspace.reset(start)
        context = {key: state[key] for key in ("scenario", "analysis", "architecture", "tests", "validation")}
        context["files"] = workspace.files()
        value = ask({**state, "attempt": attempt}, "implement", Implementation, context)
        error = value.get("error", "")
        candidate = start
        if not error:
            try:
                candidate = workspace.apply(Implementation.model_validate(value).edits)
                if candidate == state["baseline"]:
                    error = "Implementation produced no changes from baseline"
            except (PolicyError, SyntaxError, OSError, subprocess.SubprocessError) as exc:
                workspace.reset(start)
                error = str(exc)
        event(state, f"implementation:{attempt}", "implementation", attempt=attempt,
              commit=candidate, rationale=value.get("rationale"), error=error)
        return {"attempt": attempt, "candidate": candidate, "error": error,
                "implementation": {"rationale": value.get("rationale"), "commit": candidate}}

    def validation(state):
        attempt = state["attempt"]
        if state["error"]:
            passed, result = False, state["error"]
        else:
            try:
                workspace.reset(state["candidate"])
                files = workspace.files()
                missing = {"shortener/app.py", "tests/test_shortener.py", "API.md"} - files.keys()
                if missing:
                    passed, result = False, "Missing required artifacts: " + ", ".join(sorted(missing))
                else:
                    passed, result = validate(workspace.tree, state["scenario"])
            except (RunnerUnavailable, PolicyError, OSError, subprocess.SubprocessError) as exc:
                result = str(exc)
                event(state, f"validation:{attempt}", "failure", stage="validate", result=result)
                return {"validation": result, "status": "blocked", "error": result}
        event(state, f"validation:{attempt}", "validated" if passed else "failure",
              stage="validate", commit=state["candidate"], result=result)
        if not passed and attempt < MAX_ATTEMPTS:
            event(state, f"repair:{attempt}", "retry", stage="implement")
        if passed:
            diff = git(workspace.tree, "diff", "--stat", state["baseline"], state["candidate"])
            sections = {k: state[k] for k in ("analysis", "architecture", "tests", "implementation")}
            lines = ["# Engineering summary", "", f'Commit {state["candidate"]}', "",
                     "## Plan, risks and assumptions", "", json.dumps(sections, indent=2), "",
                     "## Changed artifacts", "", diff, "", "## Validation", "", result, "",
                     "## Limitations", "", "Static checks are heuristic. Human review is required for release."]
            report.write_text("\n".join(lines) + "\n")
        return {"validation": result, "status": "validated" if passed else "failed",
                "error": "" if passed else result}

    def after_validation(state):
        if state["status"] == "validated":
            return "approve"
        if state["status"] == "blocked" or state["attempt"] >= MAX_ATTEMPTS:
            return "stop"
        return "implement"

    def approve(state):
        answer = interrupt({"kind": "approval", "revision": state["revision"], "commit": state["candidate"],
                            "summary": str(report)})
        decision = answer["decision"]
        if decision == "approve" and (answer.get("commit") != state["candidate"]
                                      or answer.get("revision") != state["revision"]):
            return {"decision": "invalid", "error": "Approval did not match the validated candidate"}
        event(state, "decision", "decision", actor=answer.get("actor"), decision=decision,
              commit=state["candidate"], requirement=answer.get("requirement"))
        if decision == "revise":
            workspace.reset(state["baseline"])
            report.unlink(missing_ok=True)
            event(state, "revision-rollback", "rollback", baseline=state["baseline"])
            return {**revised(state, requirement=answer["requirement"]), "decision": decision}
        return {"decision": decision}

    def promote(state):
        try:
            workspace.promote(state["baseline"], state["candidate"])
        except (ValueError, subprocess.SubprocessError) as exc:
            return {"status": "promotion_failed", "error": str(exc)}
        event(state, "approved", "approved", commit=state["candidate"])
        return {"status": "approved"}

    def stop(state):
        workspace.reset(state["baseline"])
        report.unlink(missing_ok=True)
        status = "rejected" if state.get("decision") == "reject" else "stopped"
        if state.get("candidate") and state["candidate"] != state["baseline"]:
            event(state, "rollback", "rollback", baseline=state["baseline"], candidate=state["candidate"])
        fallback = {
            "status": status, "revision": state["revision"], "baseline": state["baseline"],
            "discarded_candidate": state.get("candidate"), "reason": state.get("error"),
            "validation": state.get("validation"),
        }
        (workspace.tree.parent / "fallback.json").write_text(json.dumps(fallback, indent=2) + "\n")
        event(state, "terminal", status, reason=state.get("error"))
        return {"status": status}

    for name, node in {
        "analyze": analyze, "clarify": clarify, "architecture": design("architecture"),
        "tests": design("tests"), "join": join, "implement": implement,
        "validate": validation, "approve": approve, "promote": promote, "stop": stop,
    }.items():
        graph.add_node(name, node)
    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", after_analysis, ["stop", "clarify", "architecture", "tests"])
    graph.add_edge("clarify", "analyze")
    graph.add_edge(["architecture", "tests"], "join")
    graph.add_conditional_edges("join", lambda s: "stop" if s["error"] else "implement",
                                ["stop", "implement"])
    graph.add_edge("implement", "validate")
    graph.add_conditional_edges("validate", after_validation, ["stop", "approve", "implement"])
    graph.add_conditional_edges("approve", lambda s: {"approve": "promote", "revise": "analyze"}
                                .get(s["decision"], "stop"), ["promote", "analyze", "stop"])
    graph.add_conditional_edges("promote", lambda s: END if s["status"] == "approved" else "stop",
                                [END, "stop"])
    graph.add_edge("stop", END)
    return graph.compile(checkpointer=checkpointer)
