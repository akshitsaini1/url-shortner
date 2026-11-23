"""SQLite lifecycle events and metrics."""

import json
import sqlite3
import time
from contextlib import closing


class Audit:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    run TEXT, key TEXT, kind TEXT, timestamp REAL, details TEXT,
                    PRIMARY KEY(run, key)
                )
            """)

    def record(self, run, key, kind, **details):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                       (run, key, kind, time.time(), json.dumps(details)))

    def read(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [
                dict(run=run, key=key, kind=kind, timestamp=stamp, details=json.loads(details))
                for run, key, kind, stamp, details in db.execute(
                    "SELECT * FROM events ORDER BY timestamp, rowid"
                )
            ]

    def metrics(self):
        starts, pending = {}, {}
        durations, recovery = [], []
        retries = rollbacks = approved = finished = 0
        for event in self.read():
            kind, run, stamp = event["kind"], event["run"], event["timestamp"]
            key = (run, event["details"].get("revision"))
            if kind == "start":
                starts[run] = stamp
            elif kind == "failure":
                pending.setdefault(key, stamp)
            elif kind == "validated" and key in pending:
                recovery.append(stamp - pending.pop(key))
            elif kind == "retry":
                retries += 1
            elif kind == "rollback":
                rollbacks += 1
            elif kind in {"approved", "rejected", "stopped"}:
                finished += 1
                approved += int(kind == "approved")
                durations.append(stamp - starts.get(run, stamp))
        return {
            "finished": finished, "success_rate": approved / finished if finished else None,
            "retries": retries, "rollbacks": rollbacks,
            "retries_per_finished_run": retries / finished if finished else None,
            "rollbacks_per_finished_run": rollbacks / finished if finished else None,
            "mean_recovery_seconds": sum(recovery) / len(recovery) if recovery else None,
            "unrecovered_failure_revisions": len(pending),
            "mean_end_to_end_seconds": sum(durations) / len(durations) if durations else None,
        }
