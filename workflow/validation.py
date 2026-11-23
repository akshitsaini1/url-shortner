"""Validate a candidate using a fixed, offline execution environment."""

import ast
import os
import subprocess
import tempfile
from pathlib import Path

from workflow.provider import Edit


class RunnerUnavailable(Exception):
    pass


class PolicyError(Exception):
    pass


def check_edits(edits, directory):
    paths = set()
    for edit in edits:
        path = Path(edit.path)
        target = directory / path
        if edit.path in paths or any(
            item.is_symlink() for item in (target, target.parent)
        ):
            raise PolicyError(f"Duplicate path or symbolic link: {path}")
        paths.add(edit.path)
        check_source(edit)


def check_source(edit: Edit):
    if not edit.path.endswith(".py"):
        return
    tree = ast.parse(edit.content, filename=edit.path)
    denied = {"subprocess", "socket", "ctypes", "pickle", "marshal", "importlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [name.name for name in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        if any(module.split(".")[0] in denied for module in modules):
            raise PolicyError(f"Disallowed import in {edit.path}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"eval", "exec", "compile", "__import__"}:
                raise PolicyError(f"Disallowed dynamic execution in {edit.path}")


def docker_validate(directory, scenario):
    image = os.environ.get("WORKFLOW_IMAGE", "shortener-validator:local")
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerUnavailable("Docker is unavailable") from exc
    image_id = probe.stdout.strip()
    if probe.returncode or not image_id.startswith("sha256:"):
        raise RunnerUnavailable("Docker daemon or prebuilt validation image unavailable")
    with tempfile.TemporaryDirectory(prefix="shortener-validation-") as temporary:
        snapshot = Path(temporary)
        snapshot.chmod(0o755)
        for name in ("shortener/app.py", "tests/test_shortener.py"):
            source = directory / name
            if source.is_symlink() or source.parent.is_symlink():
                raise PolicyError(f"Symbolic link: {name}")
            if source.is_file():
                target = snapshot / name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(source.read_bytes())
        acceptance = Path(__file__).resolve().parents[1] / "tests/test_shortener.py"
        (snapshot / "test_acceptance.py").write_bytes(acceptance.read_bytes())
        if scenario == "ambiguous":
            strict = Path(__file__).resolve().parents[1] / "scenarios/test_strict.py.txt"
            (snapshot / "test_strict.py").write_bytes(strict.read_bytes())
        with tempfile.TemporaryFile() as output:
            name = "shortener-" + snapshot.name
            try:
                result = subprocess.run(
                    ["docker", "run", "--rm", "--name", name, "--pull=never",
                     "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--user=65534:65534",
                     "--memory=256m", "--cpus=1", "--pids-limit=64",
                     "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777",
                     "--log-driver=none",
                     "--mount", f"type=bind,src={snapshot},dst=/candidate,readonly",
                     "--workdir", "/candidate", "-e", "PYTHONDONTWRITEBYTECODE=1",
                     "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "-e", "PYTHONPATH=/candidate",
                     "--entrypoint", "python", image_id, "-m", "pytest", "-q",
                     "-c", "/dev/null", "-p", "no:cacheprovider", "."],
                    stdout=output, stderr=subprocess.STDOUT, timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
                return False, f"Validation timed out after {exc.timeout}s"
            if result.returncode in {125, 126, 127}:
                raise RunnerUnavailable("Docker could not start the validation process")
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 12_000))
            return result.returncode == 0, output.read().decode(errors="replace")
