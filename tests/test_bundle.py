from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from patchgym.bundle import (
    BundleError,
    compose_description,
    load_bundle,
    resolve_repository_url,
    validate_hidden_test_paths,
)
from patchgym.models import TaskSpec


def manifest(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "task_id": "example-task",
        "repository": {
            "url": "repo.bundle",
            "base_commit": "a" * 40,
        },
        "description": {"problem": "description.md"},
        "patches": {"gold": "patch.diff", "tests": "test.patch"},
        "environment": {
            "kind": "build",
            "base_image": "python:3.11",
            "workdir": "/workspace",
        },
        "runner": {"command_template": "pytest -q {test_id}"},
        "tests": {"fail_to_pass": ["test_bug"], "pass_to_pass": ["test_old"]},
    }
    value.update(overrides)
    return value


def write_bundle(root: Path, value: dict[str, object] | None = None) -> None:
    root.mkdir()
    (root / "task.json").write_text(json.dumps(value or manifest()), encoding="utf-8")
    for name in ("description.md", "patch.diff", "test.patch", "repo.bundle"):
        (root / name).write_text(name, encoding="utf-8")


def test_load_and_compose_description(tmp_path: Path) -> None:
    value = manifest(description={"problem": "description.md", "requirements": "requirements.md"})
    write_bundle(tmp_path / "bundle", value)
    (tmp_path / "bundle" / "requirements.md").write_text("Do the thing.", encoding="utf-8")

    bundle, files = load_bundle(tmp_path / "bundle")

    assert bundle.task_id == "example-task"
    assert "# Problem statement" in compose_description(files)
    assert "# Requirements" in compose_description(files)


def test_rejects_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "bundle"
    write_bundle(root, manifest(description={"problem": "../outside.md"}))

    with pytest.raises(BundleError, match="escapes"):
        load_bundle(root)


@pytest.mark.parametrize("commit", ["abc", "z" * 40, "a" * 39])
def test_requires_full_hex_commit(commit: str) -> None:
    value = manifest(repository={"url": "repo", "base_commit": commit})
    with pytest.raises(ValidationError, match="40-character"):
        TaskSpec.model_validate(value)


def test_rejects_duplicate_or_overlapping_tests() -> None:
    value = manifest(tests={"fail_to_pass": ["same"], "pass_to_pass": ["same"]})
    with pytest.raises(ValidationError, match="both groups"):
        TaskSpec.model_validate(value)


def test_configuration_hash_is_stable() -> None:
    first = TaskSpec.model_validate(manifest())
    second = TaskSpec.model_validate(json.loads(json.dumps(manifest(), sort_keys=True)))
    assert first.configuration_hash() == second.configuration_hash()


def test_hidden_paths_are_safe_and_affect_configuration_hash() -> None:
    base = TaskSpec.model_validate(manifest())
    hidden = TaskSpec.model_validate(
        manifest(
            tests={
                "fail_to_pass": ["test_bug"],
                "pass_to_pass": ["test_old"],
                "hidden_paths": ["tests/test_bug.py", "tests/test_old.py"],
            }
        )
    )
    assert hidden.configuration_hash() != base.configuration_hash()

    for unsafe in ("../secret.py", "/secret.py", ".git/objects", "tests/*.py", "tests\\x.py"):
        value = manifest(
            tests={
                "fail_to_pass": ["test_bug"],
                "hidden_paths": [unsafe],
            }
        )
        with pytest.raises(ValidationError, match="hidden paths"):
            TaskSpec.model_validate(value)


def test_test_patch_paths_must_be_hidden(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    write_bundle(root)
    (root / "test.patch").write_text(
        "diff --git a/tests/test_secret.py b/tests/test_secret.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_secret.py\n",
        encoding="utf-8",
    )

    bundle, files = load_bundle(root)
    with pytest.raises(BundleError, match="tests.hidden_paths"):
        validate_hidden_test_paths(bundle, files)

    value = manifest(
        tests={
            "fail_to_pass": ["test_bug"],
            "pass_to_pass": ["test_old"],
            "hidden_paths": ["tests"],
        }
    )
    (root / "task.json").write_text(json.dumps(value), encoding="utf-8")
    bundle, files = load_bundle(root)
    validate_hidden_test_paths(bundle, files)
    assert bundle.tests.hidden_paths == ["tests"]


def test_hidden_path_coverage_is_optional_for_non_workspace_solvers(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    write_bundle(root)
    bundle, files = load_bundle(root)
    assert bundle.tests.hidden_paths == []
    with pytest.raises(BundleError, match="workspace-reading"):
        validate_hidden_test_paths(bundle, files)


def test_rejects_dockerfile_control_values() -> None:
    value = manifest(
        environment={
            "kind": "build",
            "base_image": "python:3.11\nRUN echo injected",
            "workdir": "/workspace",
        }
    )
    with pytest.raises(ValidationError, match="whitespace"):
        TaskSpec.model_validate(value)


def test_rejects_local_repository_outside_bundle(tmp_path: Path) -> None:
    outside = tmp_path / "repo.bundle"
    outside.write_text("repo", encoding="utf-8")
    root = tmp_path / "bundle"
    write_bundle(root, manifest(repository={"url": "../repo.bundle", "base_commit": "a" * 40}))
    bundle, files = load_bundle(root)

    with pytest.raises(BundleError, match="inside the bundle"):
        resolve_repository_url(bundle, files)


def test_shipped_bundles_and_reports_are_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    tiny, _ = load_bundle(root / "examples" / "tiny")
    pro, _ = load_bundle(root / "examples" / "swebench-pro-navidrome")
    assert tiny.task_id == "tiny-divide-by-zero"
    assert pro.tests.fail_to_pass == ["TestLastFM", "TestListenBrainz", "TestSpotify"]

    for report_path in (root / "artifacts").glob("*.json"):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["schema_version"] == 1
        assert isinstance(report["resolved"], bool)
        assert report["tests"]
