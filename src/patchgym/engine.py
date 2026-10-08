from __future__ import annotations

import json
import shlex
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

from .bundle import (
    BundleError,
    compose_description,
    resolve_repository_url,
    validate_hidden_test_paths,
)
from .db import Database
from .docker import CommandResult, Container, DockerClient, DockerError, run_process
from .llm import LLMSolverConfig, LLMSolverError, run_llm_solver
from .models import (
    BuildEnvironment,
    BundleFiles,
    InitState,
    TaskSpec,
    TestResult,
    TestStatus,
)
from .state import StateError, load_state, save_state
from .util import atomic_write_json, redact_url, utc_now


class TaskError(RuntimeError):
    pass


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")


def _write_result(log_path: Path, result: CommandResult, display: str | None = None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {display or shlex.join(result.command)}\n")
        if result.stdout:
            handle.write(result.stdout)
            if not result.stdout.endswith("\n"):
                handle.write("\n")
        if result.stderr:
            handle.write(result.stderr)
            if not result.stderr.endswith("\n"):
                handle.write("\n")
        handle.write(
            f"[exit={result.exit_code} timeout={result.timed_out} "
            f"duration_ms={result.duration_ms}]\n"
        )


def _sanitization_command(workdir: str, commit: str) -> str:
    quoted_dir = shlex.quote(workdir)
    quoted_commit = shlex.quote(commit)
    return " && ".join(
        [
            f"cd {quoted_dir}",
            f"git checkout --detach {quoted_commit}",
            f"git reset --hard {quoted_commit}",
            "git clean -fd",
            "for remote in $(git remote); do git remote remove \"$remote\"; done",
            "git for-each-ref --format='delete %(refname)' | git update-ref --stdin",
            "git reflog expire --expire=now --all",
            "git gc --prune=now",
            f"test \"$(git rev-parse HEAD)\" = {quoted_commit}",
            "test -z \"$(git status --porcelain --untracked-files=no)\"",
        ]
    )


def _write_dockerfile(
    path: Path,
    *,
    base: str,
    shell: str,
    workdir: str,
    commit: str,
    setup_commands: Sequence[str],
    copy_repository: bool,
) -> None:
    lines = [f"FROM {base}", f"SHELL [{json.dumps(shell)}, \"-c\"]"]
    if copy_repository:
        lines += [f"COPY repository {workdir}", f"WORKDIR {workdir}"]
    for command in setup_commands:
        lines.append(f"RUN {command}")
    lines.append(f"RUN {_sanitization_command(workdir, commit)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fetch_repository(source: str, commit: str, destination: Path, log_path: Path) -> None:
    destination.mkdir(parents=True)
    commands = [
        ["git", "init", str(destination)],
        ["git", "-C", str(destination), "fetch", "--depth=1", source, commit],
        ["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"],
    ]
    for command in commands:
        result = run_process(command, timeout=900)
        shown = shlex.join([redact_url(part) for part in command])
        _write_result(log_path, result, shown)
        if result.exit_code != 0:
            raise TaskError(f"failed to prepare repository: {result.output.strip()}")
    actual = run_process(["git", "-C", str(destination), "rev-parse", "HEAD"], check=True)
    if actual.stdout.strip().lower() != commit:
        raise TaskError(f"repository resolved to {actual.stdout.strip()}, expected {commit}")


def initialize_task(
    bundle: TaskSpec,
    files: BundleFiles,
    state_dir: Path,
    artifact_dir: Path,
    *,
    force: bool = False,
) -> tuple[InitState, dict[str, Any]]:
    log_path = artifact_dir / "init.log"
    docker = DockerClient(log_path)
    docker.require_daemon()
    bundle_hash = bundle.configuration_hash()
    image_ref = f"patchgym/{_slug(bundle.task_id)}:{bundle_hash[:12]}"

    if not force:
        try:
            existing_state = load_state(state_dir, bundle)
        except StateError:
            existing_state = None
        if existing_state is not None:
            existing = docker.command(
                ["image", "inspect", existing_state.image_id, "--format", "{{.Id}}"]
            )
        else:
            existing = None
        if existing_state is not None and existing and existing.exit_code == 0:
            state = existing_state
            report = {"cached": True, **state.model_dump(mode="json")}
            atomic_write_json(artifact_dir / "report.json", report)
            return state, report

    with tempfile.TemporaryDirectory(prefix="patchgym-init-") as temporary:
        context = Path(temporary)
        environment = bundle.environment
        source_digest: str | None = None
        if isinstance(environment, BuildEnvironment):
            repository = context / "repository"
            source = resolve_repository_url(bundle, files)
            _fetch_repository(source, bundle.repository.base_commit, repository, log_path)
            _write_dockerfile(
                context / "Dockerfile",
                base=environment.base_image,
                shell=environment.shell,
                workdir=environment.workdir,
                commit=bundle.repository.base_commit,
                setup_commands=environment.setup_commands,
                copy_repository=True,
            )
            source_image = environment.base_image
        else:
            docker.pull(environment.image, environment.platform)
            source_image_id = docker.inspect_image_id(environment.image)
            source_digest = docker.inspect_digest(environment.image)
            pinned_alias = f"patchgym/source-{bundle_hash[:16]}:pinned"
            docker.tag(source_image_id, pinned_alias)
            _write_dockerfile(
                context / "Dockerfile",
                base=pinned_alias,
                shell=environment.shell,
                workdir=environment.workdir,
                commit=bundle.repository.base_commit,
                setup_commands=[],
                copy_repository=False,
            )
            source_image = environment.image

        image_id = docker.build(
            context,
            image_ref,
            platform=environment.platform,
            timeout=7200,
        )

    with Container(
        docker,
        image_id,
        shell=bundle.environment.shell,
        workdir=bundle.environment.workdir,
        resources=bundle.resources,
    ) as container:
        check = container.exec(
            " && ".join(
                [
                    f"cd {shlex.quote(bundle.environment.workdir)}",
                    "test -d .git",
                    "test \"$(git rev-parse HEAD)\" = "
                    f"{shlex.quote(bundle.repository.base_commit)}",
                    "test -z \"$(git status --porcelain --untracked-files=no)\"",
                    f"test -x {shlex.quote(bundle.environment.shell)}",
                ]
            ),
            timeout=60,
        )
        if check.exit_code != 0:
            raise TaskError(f"baseline image verification failed: {check.output.strip()}")

    state = InitState(
        task_id=bundle.task_id,
        bundle_hash=bundle_hash,
        base_commit=bundle.repository.base_commit,
        image_id=image_id,
        image_ref=image_ref,
        source_image=source_image,
        source_digest=source_digest,
        platform=bundle.environment.platform,
        initialized_at=utc_now(),
    )
    state_file = save_state(state_dir, state)
    report = {"cached": False, "state_path": str(state_file), **state.model_dump(mode="json")}
    atomic_write_json(artifact_dir / "report.json", report)
    return state, report


def _reset(container: Container, bundle: TaskSpec) -> None:
    result = container.exec(
        f"cd {shlex.quote(bundle.environment.workdir)} && "
        "git reset --hard HEAD && git clean -fd",
        timeout=120,
    )
    if result.exit_code != 0:
        raise TaskError(f"failed to reset repository: {result.output.strip()}")


def _apply_patch(
    container: Container,
    bundle: TaskSpec,
    patch: Path,
    remote_name: str,
) -> None:
    if patch.stat().st_size == 0:
        return
    destination = f"/tmp/patchgym-{remote_name}.patch"
    container.copy_in(patch, destination)
    command = (
        f"cd {shlex.quote(bundle.environment.workdir)} && "
        f"git apply --check {shlex.quote(destination)} && "
        f"git apply {shlex.quote(destination)}"
    )
    result = container.exec(command, timeout=120)
    if result.exit_code != 0:
        raise TaskError(f"could not apply {remote_name}: {result.output.strip()}")


def _test_command(bundle: TaskSpec, test_id: str) -> str:
    rendered = bundle.runner.command_template.replace("{test_id}", shlex.quote(test_id))
    return f"cd {shlex.quote(bundle.environment.workdir)} && {rendered}"


def _run_tests(
    container: Container,
    bundle: TaskSpec,
    db: Database,
    command_id: str,
    artifact_dir: Path,
    *,
    phase: Literal["baseline", "gold", "candidate"],
    repetition: int,
) -> list[TestResult]:
    results: list[TestResult] = []
    groups = [
        ("pass_to_pass", bundle.tests.pass_to_pass),
        ("fail_to_pass", bundle.tests.fail_to_pass),
    ]
    log_path = artifact_dir / f"{phase}-{repetition}-tests.log"
    for group, test_ids in groups:
        for test_id in test_ids:
            result = container.exec(
                _test_command(bundle, test_id), timeout=bundle.runner.timeout_seconds
            )
            _write_result(log_path, result, f"test [{group}] {test_id}")
            if result.timed_out:
                status = TestStatus.TIMEOUT
            elif result.exit_code == 0:
                status = TestStatus.PASSED
            elif result.exit_code in {126, 127}:
                status = TestStatus.ERROR
            else:
                status = TestStatus.FAILED
            normalized = TestResult(
                test_id=test_id,
                group=group,
                phase=phase,
                repetition=repetition,
                status=status,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                log_path=str(log_path),
            )
            db.add_test_result(command_id, normalized)
            results.append(normalized)
    return results


def _result_dict(result: TestResult) -> dict[str, Any]:
    return result.model_dump(mode="json")


def _has_infrastructure_error(results: Iterable[TestResult]) -> bool:
    return any(result.status in {TestStatus.TIMEOUT, TestStatus.ERROR} for result in results)


def validate_task(
    bundle: TaskSpec,
    files: BundleFiles,
    state: InitState,
    db: Database,
    command_id: str,
    artifact_dir: Path,
    *,
    repetitions: int,
) -> dict[str, Any]:
    if repetitions < 1:
        raise TaskError("repeat count must be at least one")
    docker = DockerClient(artifact_dir / "docker.log")
    docker.require_daemon()
    all_results: list[TestResult] = []
    for repetition in range(1, repetitions + 1):
        with Container(
            docker,
            state.image_id,
            shell=bundle.environment.shell,
            workdir=bundle.environment.workdir,
            resources=bundle.resources,
        ) as baseline:
            _reset(baseline, bundle)
            _apply_patch(baseline, bundle, files.test_patch, "test")
            all_results += _run_tests(
                baseline,
                bundle,
                db,
                command_id,
                artifact_dir,
                phase="baseline",
                repetition=repetition,
            )
        with Container(
            docker,
            state.image_id,
            shell=bundle.environment.shell,
            workdir=bundle.environment.workdir,
            resources=bundle.resources,
        ) as gold:
            _reset(gold, bundle)
            _apply_patch(gold, bundle, files.gold_patch, "gold")
            _apply_patch(gold, bundle, files.test_patch, "test")
            all_results += _run_tests(
                gold,
                bundle,
                db,
                command_id,
                artifact_dir,
                phase="gold",
                repetition=repetition,
            )

    baseline_ok = all(
        (
            result.status == TestStatus.PASSED
            if result.group == "pass_to_pass"
            else result.status == TestStatus.FAILED
        )
        for result in all_results
        if result.phase == "baseline"
    )
    gold_ok = all(
        result.status == TestStatus.PASSED for result in all_results if result.phase == "gold"
    )
    infrastructure_error = _has_infrastructure_error(all_results)
    valid = baseline_ok and gold_ok and not infrastructure_error
    report = {
        "schema_version": 1,
        "command_id": command_id,
        "task_id": bundle.task_id,
        "bundle_hash": bundle.configuration_hash(),
        "image_id": state.image_id,
        "repetitions": repetitions,
        "valid": valid,
        "evaluation_error": infrastructure_error,
        "baseline_ok": baseline_ok,
        "gold_ok": gold_ok,
        "tests": [_result_dict(result) for result in all_results],
    }
    atomic_write_json(artifact_dir / "report.json", report)
    return report


def _prepare_solver_workspace(container: Container, bundle: TaskSpec) -> str:
    if not bundle.tests.hidden_paths:
        raise TaskError(
            "workspace-reading solvers require tests.hidden_paths so evaluation tests can be hidden"
        )
    quoted_dir = shlex.quote(bundle.environment.workdir)
    quoted_paths = " ".join(shlex.quote(path) for path in bundle.tests.hidden_paths)
    original = shlex.quote(bundle.repository.base_commit)
    command = " && ".join(
        [
            f"cd {quoted_dir}",
            "git checkout --quiet --orphan patchgym-solver-root",
            f"rm -rf -- {quoted_paths}",
            "git add -u",
            "git -c user.name=patchgym -c user.email=patchgym@example.invalid "
            "-c core.hooksPath=/dev/null commit --quiet --allow-empty "
            "-m 'sanitized solver baseline'",
            "solver_base=$(git rev-parse HEAD)",
            "git checkout --quiet --detach \"$solver_base\"",
            "git for-each-ref --format='delete %(refname)' | git update-ref --stdin",
            "git reflog expire --expire=now --all",
            "rm -rf .git/logs",
            "rm -f .git/ORIG_HEAD .git/FETCH_HEAD .git/MERGE_HEAD .git/CHERRY_PICK_HEAD",
            "git gc --prune=now --quiet",
            f"! git cat-file -e {original}^{{commit}} 2>/dev/null",
            "test -z \"$(git status --porcelain --untracked-files=no)\"",
            "printf '%s' \"$solver_base\"",
        ]
    )
    result = container.exec(command, timeout=300)
    solver_base = result.stdout.strip()
    if result.exit_code != 0 or not solver_base:
        raise TaskError(f"failed to create redacted solver workspace: {result.output.strip()}")
    return solver_base


def _capture_patch(
    container: Container,
    bundle: TaskSpec,
    destination: Path,
    *,
    base_commit: str | None = None,
) -> None:
    anchor = base_commit or bundle.repository.base_commit
    command = (
        f"cd {shlex.quote(bundle.environment.workdir)} && "
        "git add -A && git diff --cached --binary --full-index "
        f"{shlex.quote(anchor)} > /tmp/candidate.patch"
    )
    result = container.exec(command, timeout=180)
    if result.exit_code != 0:
        raise TaskError(f"could not capture solver changes: {result.output.strip()}")
    container.copy_out("/tmp/candidate.patch", destination)


def _group_report(results: Sequence[TestResult]) -> dict[str, dict[str, list[str]]]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for group in ("pass_to_pass", "fail_to_pass"):
        grouped[group] = {status.value: [] for status in TestStatus}
    for result in results:
        grouped[result.group][result.status.value].append(result.test_id)
    return grouped


def run_task(
    bundle: TaskSpec,
    files: BundleFiles,
    state: InitState,
    db: Database,
    command_id: str,
    artifact_dir: Path,
    *,
    solver: Literal["noop", "patch", "exec", "llm"],
    candidate_patch: Path | None = None,
    solver_command: str | None = None,
    solver_files: Sequence[Path] = (),
    solver_timeout: int = 1800,
    allow_network: bool = False,
    solver_environment: dict[str, str] | None = None,
    llm_model: str = "openai/gpt-5.6-terra",
    llm_reasoning_effort: str = "medium",
    llm_max_turns: int = 20,
    llm_api_base: str | None = None,
) -> dict[str, Any]:
    if solver in {"exec", "llm"}:
        try:
            validate_hidden_test_paths(bundle, files)
        except BundleError as exc:
            raise TaskError(str(exc)) from exc
    docker = DockerClient(artifact_dir / "docker.log")
    docker.require_daemon()
    candidate = artifact_dir / "candidate.patch"
    solver_result: dict[str, Any] = {
        "kind": solver,
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 0,
        "network_enabled": allow_network,
        "workspace_network_enabled": allow_network,
        "controller_network_required": solver == "llm",
        "environment_names": sorted((solver_environment or {}).keys()),
    }

    if solver == "noop":
        candidate.write_text("", encoding="utf-8")
    elif solver == "patch":
        if candidate_patch is None or not candidate_patch.is_file():
            raise TaskError("--candidate-patch is required for the patch solver")
        candidate.write_bytes(candidate_patch.read_bytes())
    elif solver in {"exec", "llm"}:
        if solver == "exec":
            if not solver_command:
                raise TaskError("--solver-command is required for the exec solver")
            duplicate_names = [path.name for path in solver_files]
            if len(set(duplicate_names)) != len(duplicate_names):
                raise TaskError("solver files must have unique file names")
            for path in solver_files:
                if not path.is_file():
                    raise TaskError(f"solver file does not exist: {path}")
        description = artifact_dir / "solver-description.md"
        description.write_text(compose_description(files), encoding="utf-8")
        environment: dict[str, str] = {}
        if solver == "exec":
            environment = dict(solver_environment or {})
            environment.update(
                {
                    "TASK_DESCRIPTION": "/task/description.md",
                    "TASK_WORKSPACE": bundle.environment.workdir,
                }
            )
        with Container(
            docker,
            state.image_id,
            shell=bundle.environment.shell,
            workdir=bundle.environment.workdir,
            resources=bundle.resources,
            network=allow_network,
            environment=environment,
        ) as solve_container:
            _reset(solve_container, bundle)
            solver_base = _prepare_solver_workspace(solve_container, bundle)
            if solver == "exec":
                solve_container.copy_in(description, "/task/description.md")
                for path in solver_files:
                    solve_container.copy_in(path.resolve(), f"/solver/{path.name}")
                result = solve_container.exec(
                    f"cd {shlex.quote(bundle.environment.workdir)} && {solver_command}",
                    timeout=solver_timeout,
                )
                _write_result(artifact_dir / "solver.log", result, "[solver command redacted]")
                solver_result.update(
                    {
                        "status": "timeout"
                        if result.timed_out
                        else ("completed" if result.exit_code == 0 else "error"),
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                        "duration_ms": result.duration_ms,
                    }
                )
            else:
                try:
                    llm_result = run_llm_solver(
                        solve_container,
                        description=description.read_text(encoding="utf-8"),
                        workdir=bundle.environment.workdir,
                        artifact_dir=artifact_dir,
                        config=LLMSolverConfig(
                            model=llm_model,
                            reasoning_effort=llm_reasoning_effort,
                            max_turns=llm_max_turns,
                            timeout_seconds=solver_timeout,
                            api_base=llm_api_base,
                        ),
                    )
                except LLMSolverError as exc:
                    raise TaskError(str(exc)) from exc
                solver_result.update(llm_result)
                solver_result["exit_code"] = None
                if llm_api_base:
                    solver_result["api_base"] = redact_url(llm_api_base)
            _capture_patch(solve_container, bundle, candidate, base_commit=solver_base)
    else:  # pragma: no cover - protected by Typer and Literal typing
        raise TaskError(f"unsupported solver: {solver}")

    test_results: list[TestResult] = []
    evaluation_error = False
    error: str | None = None
    patch_applied = False
    try:
        with Container(
            docker,
            state.image_id,
            shell=bundle.environment.shell,
            workdir=bundle.environment.workdir,
            resources=bundle.resources,
        ) as judge:
            _reset(judge, bundle)
            _apply_patch(judge, bundle, candidate, "candidate")
            patch_applied = True
            _apply_patch(judge, bundle, files.test_patch, "test")
            test_results = _run_tests(
                judge,
                bundle,
                db,
                command_id,
                artifact_dir,
                phase="candidate",
                repetition=1,
            )
            evaluation_error = _has_infrastructure_error(test_results)
    except (TaskError, DockerError) as exc:
        evaluation_error = True
        error = str(exc)

    resolved = bool(test_results) and not evaluation_error and all(
        result.status == TestStatus.PASSED for result in test_results
    )
    report = {
        "schema_version": 1,
        "command_id": command_id,
        "task_id": bundle.task_id,
        "bundle_hash": bundle.configuration_hash(),
        "image_id": state.image_id,
        "solver": solver_result,
        "candidate_patch": str(candidate),
        "patch_applied": patch_applied,
        "resolved": resolved,
        "evaluation_error": evaluation_error,
        "error": error,
        "groups": _group_report(test_results),
        "tests": [_result_dict(result) for result in test_results],
    }
    atomic_write_json(artifact_dir / "report.json", report)
    return report
