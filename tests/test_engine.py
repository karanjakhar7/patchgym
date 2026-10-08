from __future__ import annotations

from pathlib import Path

from patchgym.docker import CommandResult
from patchgym.engine import (
    _capture_patch,
    _group_report,
    _prepare_solver_workspace,
    _sanitization_command,
    _test_command,
)
from patchgym.models import TaskSpec
from patchgym.models import TestResult as ResultModel
from patchgym.models import TestStatus as Status

from .test_bundle import manifest


def test_test_identifier_is_shell_quoted() -> None:
    bundle = TaskSpec.model_validate(manifest())
    command = _test_command(bundle, "tests/test_file.py::test_value[param with spaces]; echo bad")
    assert "'tests/test_file.py::test_value[param with spaces]; echo bad'" in command
    assert command.endswith("'tests/test_file.py::test_value[param with spaces]; echo bad'")


def test_sanitization_removes_remote_refs_and_reflogs() -> None:
    command = _sanitization_command("/workspace", "a" * 40)
    assert "git remote remove" in command
    assert "git update-ref --stdin" in command
    assert "git reflog expire" in command
    assert "git gc --prune=now" in command


def test_group_report_keeps_all_status_buckets() -> None:
    results = [
        ResultModel(
            test_id="a",
            group="fail_to_pass",
            phase="candidate",
            status=Status.PASSED,
            exit_code=0,
            duration_ms=1,
            log_path="log",
        )
    ]
    grouped = _group_report(results)
    assert grouped["fail_to_pass"]["passed"] == ["a"]
    assert grouped["fail_to_pass"]["failed"] == []
    assert grouped["pass_to_pass"]["timeout"] == []


def test_patch_capture_is_anchored_to_configured_base(tmp_path: Path) -> None:
    bundle = TaskSpec.model_validate(manifest())

    class FakeContainer:
        def __init__(self) -> None:
            self.command = ""

        def exec(self, command: str, **kwargs):  # type: ignore[no-untyped-def]
            self.command = command
            return CommandResult(("docker",), 0, "", "", 1)

        def copy_out(self, source, destination):  # type: ignore[no-untyped-def]
            destination.write_text("", encoding="utf-8")

    container = FakeContainer()
    _capture_patch(container, bundle, tmp_path / "candidate.patch")  # type: ignore[arg-type]
    assert "a" * 40 in container.command
    assert " full-index HEAD" not in container.command


def test_solver_workspace_removes_hidden_paths_and_prunes_original_history() -> None:
    value = manifest(
        tests={
            "fail_to_pass": ["test_bug"],
            "pass_to_pass": ["test_old"],
            "hidden_paths": ["tests/test_bug.py", "tests/test_old.py"],
        }
    )
    bundle = TaskSpec.model_validate(value)

    class FakeContainer:
        def __init__(self) -> None:
            self.command = ""

        def exec(self, command: str, **kwargs):  # type: ignore[no-untyped-def]
            self.command = command
            return CommandResult(("docker",), 0, "b" * 40, "", 1)

    container = FakeContainer()
    solver_base = _prepare_solver_workspace(container, bundle)  # type: ignore[arg-type]
    assert solver_base == "b" * 40
    assert "git checkout --quiet --orphan" in container.command
    assert "tests/test_bug.py" in container.command
    assert "git reflog expire" in container.command
    assert "git gc --prune=now" in container.command
    assert f"! git cat-file -e {'a' * 40}" in container.command


def test_patch_capture_can_use_redacted_solver_base(tmp_path: Path) -> None:
    bundle = TaskSpec.model_validate(manifest())

    class FakeContainer:
        def __init__(self) -> None:
            self.command = ""

        def exec(self, command: str, **kwargs):  # type: ignore[no-untyped-def]
            self.command = command
            return CommandResult(("docker",), 0, "", "", 1)

        def copy_out(self, source, destination):  # type: ignore[no-untyped-def]
            destination.write_text("", encoding="utf-8")

    container = FakeContainer()
    _capture_patch(  # type: ignore[arg-type]
        container,
        bundle,
        tmp_path / "candidate.patch",
        base_commit="b" * 40,
    )
    assert "b" * 40 in container.command
    assert "a" * 40 not in container.command
