from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from .bundle import BundleError, load_bundle
from .db import Database
from .docker import DockerError, allowed_environment
from .engine import TaskError, initialize_task, run_task, validate_task
from .state import StateError, load_state
from .util import command_id, load_dotenv


class SolverKind(StrEnum):
    noop = "noop"
    patch = "patch"
    exec = "exec"
    llm = "llm"


class ReasoningEffort(StrEnum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"


class Context:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.expanduser().resolve()
        self.db = Database(self.state_dir)


app = typer.Typer(
    name="patchgym",
    help="Build, validate, and evaluate isolated coding tasks.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)


@app.callback()
def main(
    ctx: typer.Context,
    state_dir: Annotated[
        Path,
        typer.Option(
            "--state-dir",
            envvar="PATCHGYM_STATE_DIR",
            help="Database, image state, logs, and report directory.",
        ),
    ] = Path(".patchgym"),
) -> None:
    ctx.obj = Context(state_dir)


def _start(ctx: Context, kind: str, target: str | None = None) -> tuple[str, Path]:
    identifier = command_id(kind)
    artifacts = ctx.state_dir / "runs" / identifier
    ctx.db.start_command(identifier, kind, artifacts, target_command_id=target)
    return identifier, artifacts


def _fail(ctx: Context, identifier: str, exc: Exception) -> None:
    ctx.db.finish_command(
        identifier,
        status="error",
        outcome=None,
        exit_code=2,
        error=str(exc),
    )
    error_console.print(f"[red]Error:[/red] {exc}")
    error_console.print(f"Command ID: [bold]{identifier}[/bold]")
    raise typer.Exit(2)


@app.command("init")
def init_command(
    ctx: typer.Context,
    bundle_path: Annotated[Path, typer.Argument(help="Bundle directory or task.json path.")],
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild even if an image is cached.")
    ] = False,
) -> None:
    """Create or pull the sanitized baseline image for a bundle."""
    context: Context = ctx.obj
    identifier, artifacts = _start(context, "init")
    try:
        bundle, files = load_bundle(bundle_path)
        bundle_hash = bundle.configuration_hash()
        context.db.attach_bundle(identifier, bundle.task_id, bundle_hash)
        state, report = initialize_task(
            bundle, files, context.state_dir, artifacts, force=force
        )
        context.db.attach_bundle(identifier, bundle.task_id, bundle_hash, state.image_id)
        context.db.finish_command(
            identifier,
            status="completed",
            outcome="initialized",
            exit_code=0,
            summary=report,
        )
    except (BundleError, StateError, TaskError, DockerError, OSError) as exc:
        _fail(context, identifier, exc)
    console.print(f"Initialized [bold]{bundle.task_id}[/bold] as {state.image_ref}")
    console.print(f"Command ID: [bold]{identifier}[/bold]")


@app.command("validate")
def validate_command(
    ctx: typer.Context,
    bundle_path: Annotated[Path, typer.Argument(help="Bundle directory or task.json path.")],
    repeat: Annotated[
        int, typer.Option("--repeat", min=1, help="Run each phase in fresh containers N times.")
    ] = 1,
) -> None:
    """Verify baseline failures, regressions, and the golden patch."""
    context: Context = ctx.obj
    identifier, artifacts = _start(context, "validate")
    try:
        bundle, files = load_bundle(bundle_path)
        state = load_state(context.state_dir, bundle)
        context.db.attach_bundle(
            identifier, bundle.task_id, bundle.configuration_hash(), state.image_id
        )
        report = validate_task(
            bundle,
            files,
            state,
            context.db,
            identifier,
            artifacts,
            repetitions=repeat,
        )
        exit_code = 0 if report["valid"] else 1
        outcome = "valid" if report["valid"] else "invalid"
        status = "error" if report["evaluation_error"] else "completed"
        if report["evaluation_error"]:
            exit_code = 2
            outcome = None
        context.db.finish_command(
            identifier,
            status=status,
            outcome=outcome,
            exit_code=exit_code,
            summary=report,
        )
    except (BundleError, StateError, TaskError, DockerError, OSError) as exc:
        _fail(context, identifier, exc)
    color = "green" if report["valid"] else "red"
    console.print(f"Validation: [{color}]{'VALID' if report['valid'] else 'INVALID'}[/{color}]")
    console.print(f"Report: {artifacts / 'report.json'}")
    console.print(f"Command ID: [bold]{identifier}[/bold]")
    raise typer.Exit(exit_code)


@app.command("run")
def run_command(
    ctx: typer.Context,
    bundle_path: Annotated[Path, typer.Argument(help="Bundle directory or task.json path.")],
    solver: Annotated[SolverKind, typer.Option("--solver", help="Solver adapter.")],
    candidate_patch: Annotated[
        Path | None, typer.Option("--candidate-patch", help="Patch used by the patch solver.")
    ] = None,
    solver_command: Annotated[
        str | None,
        typer.Option("--solver-command", help="Command run inside the solver container."),
    ] = None,
    solver_file: Annotated[
        list[Path] | None,
        typer.Option("--solver-file", help="File copied to /solver; may be repeated."),
    ] = None,
    solver_timeout: Annotated[
        int, typer.Option("--solver-timeout", min=1, help="Solver timeout in seconds.")
    ] = 1800,
    model: Annotated[
        str,
        typer.Option("--model", help="LiteLLM model identifier used by the llm solver."),
    ] = "openai/gpt-5.6-terra",
    reasoning_effort: Annotated[
        ReasoningEffort,
        typer.Option("--reasoning-effort", help="LLM reasoning effort when supported."),
    ] = ReasoningEffort.medium,
    max_turns: Annotated[
        int, typer.Option("--max-turns", min=1, help="Maximum LiteLLM completion turns.")
    ] = 20,
    api_base: Annotated[
        str | None,
        typer.Option("--api-base", help="Optional LiteLLM provider or gateway base URL."),
    ] = None,
    allow_network: Annotated[
        bool,
        typer.Option("--allow-network", help="Allow solver network access (contamination risk)."),
    ] = False,
    pass_env: Annotated[
        list[str] | None,
        typer.Option("--pass-env", help="Allowlist a host environment variable; may be repeated."),
    ] = None,
    skip_validation: Annotated[
        bool,
        typer.Option("--skip-validation", help="Run without a matching successful validation."),
    ] = False,
) -> None:
    """Run a solver and grade its exported patch in a fresh container."""
    context: Context = ctx.obj
    identifier, artifacts = _start(context, "run")
    try:
        bundle, files = load_bundle(bundle_path)
        state = load_state(context.state_dir, bundle)
        bundle_hash = bundle.configuration_hash()
        context.db.attach_bundle(identifier, bundle.task_id, bundle_hash, state.image_id)
        if not skip_validation and not context.db.latest_valid_validation(
            bundle.task_id, bundle_hash, state.image_id
        ):
            raise StateError(
                "no successful validation matches this bundle and image; run 'task validate' "
                "or pass --skip-validation"
            )
        if solver == SolverKind.llm:
            if pass_env:
                raise TaskError(
                    "--pass-env is not used by the llm solver; provider credentials remain "
                    "in the host environment"
                )
            if candidate_patch or solver_command or solver_file:
                raise TaskError(
                    "--candidate-patch, --solver-command, and --solver-file are not valid "
                    "for the llm solver"
                )
            environment = {}
        else:
            environment = allowed_environment(pass_env or [])
        report = run_task(
            bundle,
            files,
            state,
            context.db,
            identifier,
            artifacts,
            solver=solver.value,
            candidate_patch=candidate_patch.expanduser().resolve() if candidate_patch else None,
            solver_command=solver_command,
            solver_files=[path.expanduser().resolve() for path in (solver_file or [])],
            solver_timeout=solver_timeout,
            allow_network=allow_network,
            solver_environment=environment,
            llm_model=model,
            llm_reasoning_effort=reasoning_effort.value,
            llm_max_turns=max_turns,
            llm_api_base=api_base,
        )
        if report["evaluation_error"]:
            exit_code, status, outcome = 2, "error", None
        elif report["resolved"]:
            exit_code, status, outcome = 0, "completed", "resolved"
        else:
            exit_code, status, outcome = 1, "completed", "unresolved"
        context.db.finish_command(
            identifier,
            status=status,
            outcome=outcome,
            exit_code=exit_code,
            summary=report,
            error=report.get("error"),
        )
    except (BundleError, StateError, TaskError, DockerError, OSError) as exc:
        _fail(context, identifier, exc)
    verdict = "RESOLVED" if report["resolved"] else "UNRESOLVED"
    color = "green" if report["resolved"] else "red"
    if report["evaluation_error"]:
        verdict, color = "EVALUATION ERROR", "red"
    console.print(f"Result: [{color}]{verdict}[/{color}]")
    console.print(f"Report: {artifacts / 'report.json'}")
    console.print(f"Command ID: [bold]{identifier}[/bold]")
    raise typer.Exit(exit_code)


def _show_table(record: dict[str, Any]) -> None:
    table = Table(show_header=False, box=None)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, key in (
        ("Command", "id"),
        ("Task", "task_id"),
        ("Type", "kind"),
        ("Status", "status"),
        ("Outcome", "outcome"),
        ("Started", "started_at"),
        ("Finished", "finished_at"),
        ("Exit code", "exit_code"),
        ("Artifacts", "artifact_dir"),
    ):
        table.add_row(label, str(record.get(key) if record.get(key) is not None else "—"))
    console.print(table)
    if record.get("error"):
        console.print(f"[red]Error:[/red] {record['error']}")
    tests = record.get("tests", [])
    if tests:
        counts: dict[tuple[str, str, str], int] = {}
        for test in tests:
            key = (test["phase"], test["test_group"], test["status"])
            counts[key] = counts.get(key, 0) + 1
        result_table = Table("Phase", "Group", "Status", "Count")
        for (phase, group, status), count in sorted(counts.items()):
            result_table.add_row(phase, group, status, str(count))
        console.print(result_table)


@app.command("show")
def show_command(
    ctx: typer.Context,
    target_command_id: Annotated[str, typer.Argument(help="Command ID to inspect.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Display one previously recorded command and its test results."""
    context: Context = ctx.obj
    identifier, _ = _start(context, "show", target_command_id)
    try:
        record = context.db.get_command(target_command_id)
        if record is None:
            raise TaskError(f"command not found: {target_command_id}")
        context.db.finish_command(
            identifier,
            status="completed",
            outcome="shown",
            exit_code=0,
            summary={"target_command_id": target_command_id},
        )
    except (TaskError, OSError) as exc:
        _fail(context, identifier, exc)
    if as_json:
        console.print_json(json.dumps(record))
    else:
        _show_table(record)


def run() -> None:
    load_dotenv()
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
