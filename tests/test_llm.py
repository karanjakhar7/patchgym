from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from patchgym.docker import CommandResult
from patchgym.llm import (
    LLMSolverConfig,
    LLMSolverError,
    _truncate,
    run_llm_solver,
    solver_messages,
)


class FakeContainer:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def exec(self, command: str, *, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(("docker",), 0, "ok\n", "", 7)


def response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )


def shell_call(call_id: str, command: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "run_shell", "arguments": json.dumps({"command": command})},
    }


def test_solver_prompt_contains_only_description_and_workspace() -> None:
    messages = solver_messages("Fix the frobnicator.", "/workspace")
    rendered = json.dumps(messages)
    assert "Fix the frobnicator." in rendered
    assert "/workspace" in rendered
    for hidden in (
        "fail_to_pass",
        "pass_to_pass",
        "test.patch",
        "patch.diff",
        "tests/test_secret.py",
    ):
        assert hidden not in rendered


def test_tool_output_preserves_head_and_tail() -> None:
    value = "a" * 15 + "middle" + "z" * 15
    truncated = _truncate(value, 20)
    assert truncated.startswith("a" * 10)
    assert truncated.endswith("z" * 10)
    assert "truncated" in truncated


def test_llm_tool_loop_executes_shell_and_records_usage(tmp_path: Path) -> None:
    replies = iter(
        [
            response(
                tool_calls=[shell_call("call-1", "python -m pytest")],
                finish_reason="tool_calls",
            ),
            response(content="Implemented and tested."),
        ]
    )
    requests: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return next(replies)

    container = FakeContainer()
    result = run_llm_solver(
        container,  # type: ignore[arg-type]
        description="Fix it.",
        workdir="/workspace",
        artifact_dir=tmp_path,
        config=LLMSolverConfig(model="provider/model"),
        completion_fn=completion,
        supports_function_calling_fn=lambda model: True,
    )

    assert container.commands == ["cd /workspace && python -m pytest"]
    assert len(requests) == 2
    assert requests[0]["drop_params"] is True
    assert requests[0]["reasoning_effort"] == "medium"
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert result["status"] == "completed"
    assert result["turns"] == 2
    assert result["tool_calls"] == 1
    assert result["usage"] == {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10}
    transcript = json.loads((tmp_path / "solver-transcript.json").read_text(encoding="utf-8"))
    assert [item["type"] for item in transcript] == ["prompt", "assistant", "tool", "assistant"]


def test_invalid_tool_arguments_are_returned_to_model(tmp_path: Path) -> None:
    bad_call = {
        "id": "call-bad",
        "type": "function",
        "function": {"name": "run_shell", "arguments": "{"},
    }
    replies = iter(
        [
            response(tool_calls=[bad_call], finish_reason="tool_calls"),
            response(content="Could not continue."),
        ]
    )
    requests: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return next(replies)

    run_llm_solver(
        FakeContainer(),  # type: ignore[arg-type]
        description="Fix it.",
        workdir="/workspace",
        artifact_dir=tmp_path,
        config=LLMSolverConfig(model="provider/model"),
        completion_fn=completion,
        supports_function_calling_fn=lambda model: True,
    )
    tool_message = requests[1]["messages"][-1]
    assert "invalid tool arguments" in tool_message["content"]


def test_rejects_model_without_native_tools(tmp_path: Path) -> None:
    with pytest.raises(LLMSolverError, match="does not support"):
        run_llm_solver(
            FakeContainer(),  # type: ignore[arg-type]
            description="Fix it.",
            workdir="/workspace",
            artifact_dir=tmp_path,
            config=LLMSolverConfig(model="provider/model"),
            completion_fn=lambda **kwargs: None,
            supports_function_calling_fn=lambda model: False,
        )


def test_provider_failure_after_tool_work_retains_partial_result(tmp_path: Path) -> None:
    calls = 0

    def completion(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(
                tool_calls=[shell_call("call-1", "printf fixed > module.py")],
                finish_reason="tool_calls",
            )
        raise RuntimeError("Authorization: sk-supersecretvalue")

    result = run_llm_solver(
        FakeContainer(),  # type: ignore[arg-type]
        description="Fix it.",
        workdir="/workspace",
        artifact_dir=tmp_path,
        config=LLMSolverConfig(model="provider/model"),
        completion_fn=completion,
        supports_function_calling_fn=lambda model: True,
    )
    assert result["status"] == "error"
    assert result["finish_reason"] == "provider_error"
    assert "supersecretvalue" not in result["error"]


def test_provider_failure_before_response_is_infrastructure_error(tmp_path: Path) -> None:
    def completion(**kwargs: Any) -> Any:
        raise RuntimeError("api_key=secret-value")

    with pytest.raises(LLMSolverError, match="LiteLLM request failed"):
        run_llm_solver(
            FakeContainer(),  # type: ignore[arg-type]
            description="Fix it.",
            workdir="/workspace",
            artifact_dir=tmp_path,
            config=LLMSolverConfig(model="provider/model"),
            completion_fn=completion,
            supports_function_calling_fn=lambda model: True,
        )
    transcript = (tmp_path / "solver-transcript.json").read_text(encoding="utf-8")
    assert "secret-value" not in transcript


def test_parallel_tool_calls_run_sequentially_before_turn_limit(tmp_path: Path) -> None:
    container = FakeContainer()
    result = run_llm_solver(
        container,  # type: ignore[arg-type]
        description="Fix it.",
        workdir="/workspace",
        artifact_dir=tmp_path,
        config=LLMSolverConfig(model="provider/model", max_turns=1),
        completion_fn=lambda **kwargs: response(
            tool_calls=[shell_call("call-1", "first"), shell_call("call-2", "second")],
            finish_reason="tool_calls",
        ),
        supports_function_calling_fn=lambda model: True,
    )
    assert container.commands == ["cd /workspace && first", "cd /workspace && second"]
    assert result["status"] == "turn_limit"
    assert result["finish_reason"] == "max_turns"
    assert result["tool_calls"] == 2
