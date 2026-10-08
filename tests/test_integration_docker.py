from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from patchgym.cli import app
from patchgym.db import Database

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "examples" / "tiny"


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    return result.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="Docker daemon is unavailable"),
]


def invoke(state: Path, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["--state-dir", str(state), *args])


def test_tiny_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    initialized = invoke(state, "init", str(BUNDLE))
    assert initialized.exit_code == 0, initialized.output

    cached = invoke(state, "init", str(BUNDLE))
    assert cached.exit_code == 0, cached.output
    cached_record = Database(state).list_commands()[0]
    cached_details = Database(state).get_command(cached_record["id"])
    assert cached_details is not None
    assert cached_details["summary"]["cached"] is True

    validated = invoke(state, "validate", str(BUNDLE))
    assert validated.exit_code == 0, validated.output

    noop = invoke(state, "run", str(BUNDLE), "--solver", "noop")
    assert noop.exit_code == 1, noop.output

    patched = invoke(
        state,
        "run",
        str(BUNDLE),
        "--solver",
        "patch",
        "--candidate-patch",
        str(BUNDLE / "patch.diff"),
    )
    assert patched.exit_code == 0, patched.output

    executable = invoke(
        state,
        "run",
        str(BUNDLE),
        "--solver",
        "exec",
        "--solver-file",
        str(BUNDLE / "example_solver.py"),
        "--solver-command",
        "test -f tests/test_visible.py && "
        "test \"$(find tests -name 'test_*.py' | wc -l | tr -d ' ')\" = 1 && "
        "test -z \"$(git fsck --unreachable 2>/dev/null)\" && "
        "python -m unittest tests.test_visible && "
        "python /solver/example_solver.py && git config user.name solver && "
        "git config user.email solver@example.invalid && git add -A && git commit -m fix",
    )
    assert executable.exit_code == 0, executable.output

    import litellm

    replies = iter(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        message={
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-fix",
                                    "type": "function",
                                    "function": {
                                        "name": "run_shell",
                                        "arguments": json.dumps(
                                            {
                                                "command": (
                                                    "test -f tests/test_visible.py && "
                                                    "test \"$(find tests -name 'test_*.py' | "
                                                    "wc -l | tr -d ' ')\" = 1 && "
                                                    "test -z \"$(git fsck --unreachable "
                                                    "2>/dev/null)\" && "
                                                    "sed -i 's/        return 0/        raise "
                                                    "ValueError(\"divisor must not be zero\")/' "
                                                    "tinycalc.py && python -m unittest "
                                                    "tests.test_visible"
                                                )
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    )
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message={"role": "assistant", "content": "Fixed and tested."},
                    )
                ],
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            ),
        ]
    )
    monkeypatch.setattr(litellm, "supports_function_calling", lambda model: True)
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: next(replies))
    llm = invoke(state, "run", str(BUNDLE), "--solver", "llm", "--model", "fake/model")
    assert llm.exit_code == 0, llm.output
    llm_record = Database(state).list_commands()[0]
    llm_details = Database(state).get_command(llm_record["id"])
    assert llm_details is not None
    assert llm_details["summary"]["solver"]["provider"] == "litellm"
    assert llm_details["summary"]["solver"]["environment_names"] == []
    assert llm_details["summary"]["solver"]["workspace_network_enabled"] is False
    transcript_path = Path(llm_details["summary"]["solver"]["transcript_path"])
    transcript = transcript_path.read_text(encoding="utf-8")
    for hidden in (
        "tests.test_zero.DivideZeroTests.test_zero_raises",
        "tests.test_existing.DivideTests.test_regular_division",
        "tests/test_existing.py",
        "tests/test_zero.py",
    ):
        assert hidden not in transcript

    initialized_state = json.loads(
        (state / "tasks" / "tiny-divide-by-zero" / "state.json").read_text(encoding="utf-8")
    )
    isolation = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            initialized_state["image_id"],
            "-c",
            "cd /workspace && test -z \"$(git remote)\" && "
            "test -z \"$(git show-ref)\" && "
            "test -z \"$(git fsck --unreachable 2>/dev/null)\" && "
            "! find / -name patch.diff -o -name test.patch -o -name task.json "
            "2>/dev/null | grep .",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert isolation.returncode == 0, isolation.stdout + isolation.stderr

    latest = Database(state).list_commands()[0]
    shown = invoke(state, "show", latest["id"], "--json")
    assert shown.exit_code == 0
    assert '"outcome": "resolved"' in shown.output


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("PATCHGYM_LIVE_LLM") != "1" or "OPENAI_API_KEY" not in os.environ,
    reason="set PATCHGYM_LIVE_LLM=1 and OPENAI_API_KEY to run the paid live smoke test",
)
def test_live_openai_solver(tmp_path: Path) -> None:
    state = tmp_path / "live-state"
    assert invoke(state, "init", str(BUNDLE)).exit_code == 0
    assert invoke(state, "validate", str(BUNDLE)).exit_code == 0
    solved = invoke(
        state,
        "run",
        str(BUNDLE),
        "--solver",
        "llm",
        "--model",
        "openai/gpt-5.6-terra",
        "--max-turns",
        "20",
    )
    assert solved.exit_code == 0, solved.output
