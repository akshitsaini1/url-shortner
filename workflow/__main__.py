"""Start, inspect, and resume a workflow from the terminal."""

import argparse
import fcntl
import getpass
import json
import re
import subprocess
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from workflow.audit import Audit
from workflow.graph import build_graph
from workflow.provider import AMBIGUOUS_REQUIREMENT, LiveProvider, MockProvider
from workflow.validation import docker_validate
from workflow.workspace import Workspace, git

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = {
    "greenfield": "Build a FastAPI URL shortener with SQLite, redirects, click counts, expiration, and tests.",
    "brownfield": "Add optional URL expiration. Preserve API compatibility and migrate existing links.",
    "ambiguous": AMBIGUOUS_REQUIREMENT,
}


def show(snapshot, workspace):
    state = snapshot.values
    if snapshot.interrupts:
        gate = snapshot.interrupts[0].value
        print(json.dumps(gate, indent=2))
        if gate["kind"] == "approval":
            diff = git(workspace.repo, "diff", "--no-ext-diff", "--no-textconv",
                       state["baseline"], state["candidate"])
            print("".join(char if char.isprintable() or char in "\n\t"
                          else ascii(char)[1:-1] for char in diff))
    else:
        print(json.dumps({k: state.get(k) for k in ("run", "revision", "status", "attempt", "error")},
                         indent=2))


def resume(parser, args, snapshot):
    if snapshot.interrupts:
        gate = snapshot.interrupts[0].value
        if gate["kind"] == "clarification":
            if args.decision != "clarify" or not args.text:
                parser.error("This run requires resume RUN clarify --text 'answer'")
            return Command(resume=args.text)
        if args.decision == "approve":
            if args.commit != gate["commit"] or args.revision != gate["revision"]:
                parser.error("Pass the commit and revision displayed by status")
        elif args.decision == "revise":
            if not args.text:
                parser.error("Pass the revised requirement with --text")
        elif args.decision != "reject":
            parser.error("This run awaits approve, reject, or revise")
        return Command(resume={"decision": args.decision, "commit": args.commit,
                               "revision": args.revision, "requirement": args.text,
                               "actor": getpass.getuser()})
    if args.decision or not snapshot.next:
        parser.error("Run has no pending work to resume")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path(".runs"))
    sub = parser.add_subparsers(dest="operation", required=True)
    start = sub.add_parser("start")
    start.add_argument("run")
    start.add_argument("scenario", choices=REQUIREMENTS)
    start.add_argument("--provider", choices=("mock", "live"), default="mock")
    start.add_argument("--requirement")
    status = sub.add_parser("status")
    status.add_argument("run")
    restart = sub.add_parser("resume")
    restart.add_argument("run")
    restart.add_argument("decision", nargs="?", choices=("clarify", "approve", "reject", "revise"))
    restart.add_argument("--text")
    restart.add_argument("--commit")
    restart.add_argument("--revision", type=int)
    sub.add_parser("metrics")
    sub.add_parser("events")
    args = parser.parse_args()

    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = state_dir / "workflow.sqlite"
    audit = Audit(database)
    if args.operation in {"metrics", "events"}:
        print(json.dumps(audit.metrics() if args.operation == "metrics" else audit.read(), indent=2))
        return 0
    if not re.fullmatch("[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", args.run):
        parser.error("Run ID must start with an ASCII letter or digit and contain up to 64 characters")
    if len(getattr(args, "text", "") or "") > 4000 or len(getattr(args, "requirement", "") or "") > 4000:
        parser.error("Requirement and clarification text are limited to 4000 characters")
    run_dir = state_dir / args.run
    live_provider = None
    if args.operation == "start" and args.provider == "live":
        live_provider = LiveProvider(ROOT / ".env")
    if args.operation == "start":
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            parser.error("Run ID already exists")
    if not run_dir.is_dir():
        parser.error("Run ID does not exist")
    workspace = Workspace(run_dir)
    with (run_dir / "lock").open("a") as lock, SqliteSaver.from_conn_string(str(database)) as saver:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another process is operating on this run")
        metadata = run_dir / "provider"
        if args.operation == "start":
            metadata.write_text(args.provider)
        provider_name = metadata.read_text().strip()
        provider = MockProvider(ROOT)
        if provider_name == "live" and args.operation != "status":
            provider = live_provider or LiveProvider(ROOT / ".env")
        graph = build_graph(provider, workspace, audit, docker_validate, saver)
        config = {"configurable": {"thread_id": args.run}, "recursion_limit": 100}
        if args.operation == "start":
            baseline = workspace.initialize(args.scenario, ROOT)
            audit.record(args.run, "start", "start", scenario=args.scenario, provider=provider_name,
                         baseline=baseline)
            graph.invoke({"run": args.run, "scenario": args.scenario, "revision": 1,
                          "requirement": args.requirement or REQUIREMENTS[args.scenario],
                          "clarification": "", "baseline": baseline, "attempt": 0,
                          "candidate": "", "validation": ""}, config, durability="sync")
        elif args.operation == "resume":
            command = resume(parser, args, graph.get_state(config))
            graph.invoke(command, config, durability="sync")
        snapshot = graph.get_state(config)
        show(snapshot, workspace)
        return 1 if snapshot.values.get("status") == "stopped" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        sys.exit(f"Workflow error: {exc}")
