"""Git operations confined to a workflow-owned repository and worktree."""

import subprocess


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.name=Shortener Workflow", "-c", "user.email=workflow@example.invalid",
         "-C", str(root), *args],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


class Workspace:
    def __init__(self, run_dir):
        self.repo = run_dir / "repository"
        self.tree = run_dir / "candidate"

    def initialize(self, scenario, root):
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        if scenario != "greenfield":
            (self.repo / "shortener").mkdir()
            app = root / ("scenarios/brownfield.py.txt" if scenario == "brownfield" else "shortener/app.py")
            (self.repo / "shortener/app.py").write_bytes(app.read_bytes())
        git(self.repo, "add", "--all")
        git(self.repo, "commit", "--allow-empty", "-m", f"{scenario} baseline")
        baseline = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "worktree", "add", "--detach", str(self.tree), baseline)
        return baseline

    def reset(self, commit):
        git(self.tree, "reset", "--hard", commit)
        git(self.tree, "clean", "-fdx")

    def files(self):
        result = {}
        for path in ("shortener/app.py", "tests/test_shortener.py", "API.md"):
            target = self.tree / path
            if target.is_file() and not target.is_symlink() and not target.parent.is_symlink():
                result[path] = target.read_text()[:30_000]
        return result

    def apply(self, edits):
        from workflow.validation import check_edits

        check_edits(edits, self.tree)
        paths = []
        for edit in edits:
            target = self.tree / edit.path
            target.parent.mkdir(exist_ok=True)
            target.write_text(edit.content)
            paths.append(edit.path)
        git(self.tree, "add", "--", *paths)
        if git(self.tree, "diff", "--cached", "--name-only"):
            git(self.tree, "commit", "-m", "Proposed workflow changes")
        return git(self.tree, "rev-parse", "HEAD")

    def promote(self, baseline, candidate):
        if git(self.repo, "rev-parse", "main") != baseline:
            raise ValueError("The baseline branch moved after the run started")
        existing = git(self.repo, "for-each-ref", "--format=%(objectname)", "refs/heads/review")
        if existing and existing != candidate:
            raise ValueError("A different commit already occupies the review branch")
        if not existing:
            git(self.repo, "update-ref", "refs/heads/review", candidate, "0" * len(baseline))
