from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from .models import BundleFiles, TaskSpec


class BundleError(ValueError):
    pass


def _git_patch_paths(path: Path) -> set[str]:
    """Return repository paths named by git-style diff headers."""
    paths: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise BundleError(f"invalid git patch header in {path.name}: {line}") from exc
        if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
            raise BundleError(f"invalid git patch header in {path.name}: {line}")
        for value in parts[2:]:
            relative = value[2:]
            candidate = PurePosixPath(relative)
            if (
                not relative
                or candidate.is_absolute()
                or any(part in {"", ".", "..", ".git"} for part in candidate.parts)
            ):
                raise BundleError(f"unsafe repository path in {path.name}: {relative}")
            paths.add(candidate.as_posix())
    return paths


def _is_covered(path: str, hidden_paths: list[str]) -> bool:
    candidate = PurePosixPath(path)
    hidden = map(PurePosixPath, hidden_paths)
    return any(candidate == item or item in candidate.parents for item in hidden)


def validate_hidden_test_paths(bundle: TaskSpec, files: BundleFiles) -> None:
    if not bundle.tests.hidden_paths:
        raise BundleError(
            "workspace-reading solvers require tests.hidden_paths so evaluation tests can be hidden"
        )
    uncovered = sorted(
        path
        for path in _git_patch_paths(files.test_patch)
        if not _is_covered(path, bundle.tests.hidden_paths)
    )
    if uncovered:
        raise BundleError(
            "tests.hidden_paths must cover every path touched by test.patch: "
            + ", ".join(uncovered)
        )


def _resolve_inside(root: Path, relative: str, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise BundleError(f"{label} must be relative to the bundle directory")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BundleError(f"{label} escapes the bundle directory: {relative}") from exc
    if not resolved.is_file():
        raise BundleError(f"{label} does not exist or is not a file: {relative}")
    return resolved


def load_bundle(path: Path) -> tuple[TaskSpec, BundleFiles]:
    root = path.expanduser().resolve()
    manifest = root / "task.json" if root.is_dir() else root
    root = manifest.parent
    if not manifest.is_file():
        raise BundleError(f"bundle manifest not found: {manifest}")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"invalid JSON in {manifest}: {exc}") from exc
    try:
        bundle = TaskSpec.model_validate(raw)
    except ValidationError as exc:
        raise BundleError(str(exc)) from exc

    problem = _resolve_inside(root, bundle.description.problem, "description.problem")
    requirements = (
        _resolve_inside(root, bundle.description.requirements, "description.requirements")
        if bundle.description.requirements
        else None
    )
    interface = (
        _resolve_inside(root, bundle.description.interface, "description.interface")
        if bundle.description.interface
        else None
    )
    gold_patch = _resolve_inside(root, bundle.patches.gold, "patches.gold")
    test_patch = _resolve_inside(root, bundle.patches.tests, "patches.tests")
    files = BundleFiles(
        root=root,
        manifest=manifest,
        problem=problem,
        requirements=requirements,
        interface=interface,
        gold_patch=gold_patch,
        test_patch=test_patch,
    )
    return bundle, files


def compose_description(files: BundleFiles) -> str:
    sections = [("Problem statement", files.problem)]
    if files.requirements:
        sections.append(("Requirements", files.requirements))
    if files.interface:
        sections.append(("Interface", files.interface))
    return "\n\n".join(
        f"# {heading}\n\n{path.read_text(encoding='utf-8').strip()}" for heading, path in sections
    ) + "\n"


def resolve_repository_url(bundle: TaskSpec, files: BundleFiles) -> str:
    value = bundle.repository.url
    if "://" in value or value.startswith("git@"):
        return value
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = files.root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(files.root)
    except ValueError as exc:
        raise BundleError(
            "local repository paths must remain inside the bundle directory"
        ) from exc
    if not resolved.is_file() and not resolved.is_dir():
        raise BundleError(f"local repository does not exist: {value}")
    return str(resolved)
