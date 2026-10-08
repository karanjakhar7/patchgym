from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .docker import Container
from .util import atomic_write_json, redact_url

MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_TOOL_TIMEOUT_SECONDS = 300


class LLMSolverError(RuntimeError):
    """A provider/configuration failure before the model began solving."""


@dataclass(frozen=True)
class LLMSolverConfig:
    model: str = "openai/gpt-5.6-terra"
    reasoning_effort: str = "medium"
    max_turns: int = 20
    timeout_seconds: int = 1800
    api_base: str | None = None


def solver_messages(description: str, workdir: str) -> list[dict[str, Any]]:
    system = (
        "You are solving a software engineering task in an isolated repository. "
        "Use the run_shell tool to inspect files, edit the implementation, and run the tests "
        "that are present in the repository. Work only inside the provided workspace. "
        "Do not attempt to find benchmark metadata, hidden evaluation tests, golden patches, "
        "external solutions, or deleted Git history. Continue until the implementation is "
        "complete, then respond with a concise summary and the tests you ran."
    )
    user = f"Workspace: {workdir}\n\nTask description:\n\n{description}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


RUN_SHELL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "Run a shell command in the repository workspace. Use it to inspect, edit, and "
            "test the repository. Commands run sequentially."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TOOL_TIMEOUT_SECONDS,
                    "description": "Optional command timeout; defaults to 120 seconds.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _json_value(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json", exclude_none=True)
        except TypeError:
            return obj.model_dump(exclude_none=True)
    if isinstance(obj, dict):
        return {key: _json_value(value) for key, value in obj.items() if value is not None}
    if isinstance(obj, (list, tuple)):
        return [_json_value(value) for value in obj]
    return obj


def _truncate(value: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    half = limit // 2
    omitted = len(value) - (half * 2)
    return f"{value[:half]}\n...[truncated {omitted} characters]...\n{value[-half:]}"


def _safe_error(exc: BaseException) -> str:
    value = redact_url(str(exc))
    value = re.sub(
        r"(?i)(authorization[=: '\"]+bearer\s+)[^\s,;}\]]+", r"\1[redacted]", value
    )
    value = re.sub(r"(?i)(authorization|api[_-]?key)([=: ]+)[^\s,;]+", r"\1\2[redacted]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", value)
    return value


def _append_solver_log(path: Path, command: str, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ [llm shell] {command}\n")
        if result["stdout"]:
            handle.write(result["stdout"])
            if not result["stdout"].endswith("\n"):
                handle.write("\n")
        if result["stderr"]:
            handle.write(result["stderr"])
            if not result["stderr"].endswith("\n"):
                handle.write("\n")
        handle.write(
            f"[exit={result['exit_code']} timeout={result['timed_out']} "
            f"duration_ms={result['duration_ms']}]\n"
        )


def _usage(response: Any) -> dict[str, int]:
    usage = _value(response, "usage")
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": int(_value(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(_value(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(_value(usage, "total_tokens", 0) or 0),
    }


def run_llm_solver(
    container: Container,
    *,
    description: str,
    workdir: str,
    artifact_dir: Path,
    config: LLMSolverConfig,
    completion_fn: Callable[..., Any] | None = None,
    supports_function_calling_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    if config.max_turns < 1:
        raise LLMSolverError("--max-turns must be at least one")
    if config.timeout_seconds < 1:
        raise LLMSolverError("--solver-timeout must be at least one second")

    if completion_fn is None or supports_function_calling_fn is None:
        import litellm

        completion_fn = completion_fn or litellm.completion
        supports_function_calling_fn = (
            supports_function_calling_fn or litellm.supports_function_calling
        )
    try:
        supports_tools = supports_function_calling_fn(config.model)
    except Exception as exc:
        raise LLMSolverError(f"could not inspect model capabilities: {_safe_error(exc)}") from exc
    if not supports_tools:
        raise LLMSolverError(
            f"LiteLLM model {config.model!r} does not support native function calling"
        )

    transcript_path = artifact_dir / "solver-transcript.json"
    log_path = artifact_dir / "solver.log"
    messages = solver_messages(description, workdir)
    transcript: list[dict[str, Any]] = [{"type": "prompt", "messages": messages.copy()}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    started = time.monotonic()
    turns = 0
    tool_calls_count = 0
    received_response = False
    status = "completed"
    finish_reason: str | None = None
    error: str | None = None

    try:
        for turn in range(1, config.max_turns + 1):
            remaining = config.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                status = "timeout"
                finish_reason = "solver_timeout"
                break
            request: dict[str, Any] = {
                "model": config.model,
                "messages": messages.copy(),
                "tools": [RUN_SHELL_TOOL],
                "tool_choice": "auto",
                "reasoning_effort": config.reasoning_effort,
                "drop_params": True,
                "timeout": max(1.0, remaining),
            }
            if config.api_base:
                request["api_base"] = config.api_base
            try:
                response = completion_fn(**request)
            except Exception as exc:
                error = _safe_error(exc)
                transcript.append({"type": "provider_error", "turn": turn, "error": error})
                if not received_response:
                    raise LLMSolverError(f"LiteLLM request failed: {error}") from exc
                status = "error"
                finish_reason = "provider_error"
                break

            received_response = True
            turns = turn
            current_usage = _usage(response)
            for key in usage:
                usage[key] += current_usage[key]
            choices = _value(response, "choices", [])
            if not choices:
                error = "LiteLLM response did not contain a completion choice"
                transcript.append({"type": "provider_error", "turn": turn, "error": error})
                status = "error"
                finish_reason = "invalid_response"
                break
            choice = choices[0]
            message = _value(choice, "message")
            finish_reason = _value(choice, "finish_reason")
            assistant_message = _json_value(message)
            messages.append(assistant_message)
            transcript.append(
                {
                    "type": "assistant",
                    "turn": turn,
                    "finish_reason": finish_reason,
                    "message": assistant_message,
                    "usage": current_usage,
                }
            )
            tool_calls = _value(message, "tool_calls", None) or []
            if not tool_calls:
                break

            for tool_call in tool_calls:
                tool_calls_count += 1
                call_id = _value(tool_call, "id")
                function = _value(tool_call, "function")
                name = _value(function, "name")
                raw_arguments = _value(function, "arguments", "{}")
                tool_result: dict[str, Any]
                try:
                    arguments = json.loads(raw_arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    tool_result = {"error": f"invalid tool arguments: {exc}"}
                else:
                    command = arguments.get("command")
                    if name != "run_shell":
                        tool_result = {"error": f"unknown tool: {name}"}
                    elif not isinstance(command, str) or not command.strip():
                        tool_result = {"error": "command must be a non-empty string"}
                    else:
                        requested_timeout = arguments.get("timeout_seconds", 120)
                        if not isinstance(requested_timeout, int) or isinstance(
                            requested_timeout, bool
                        ):
                            requested_timeout = 120
                        remaining = max(
                            1, int(config.timeout_seconds - (time.monotonic() - started))
                        )
                        timeout = min(
                            max(1, requested_timeout), MAX_TOOL_TIMEOUT_SECONDS, remaining
                        )
                        result = container.exec(
                            f"cd {shlex.quote(workdir)} && {command}", timeout=timeout
                        )
                        tool_result = {
                            "exit_code": result.exit_code,
                            "timed_out": result.timed_out,
                            "duration_ms": result.duration_ms,
                            "stdout": _truncate(result.stdout),
                            "stderr": _truncate(result.stderr),
                        }
                        _append_solver_log(log_path, command, tool_result)
                rendered_result = json.dumps(tool_result, sort_keys=True)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": rendered_result,
                }
                messages.append(tool_message)
                transcript.append(
                    {
                        "type": "tool",
                        "turn": turn,
                        "call_id": call_id,
                        "name": name,
                        "arguments": raw_arguments,
                        "result": tool_result,
                    }
                )
        else:
            status = "turn_limit"
            finish_reason = "max_turns"
    finally:
        atomic_write_json(transcript_path, transcript)

    return {
        "provider": "litellm",
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "max_turns": config.max_turns,
        "turns": turns,
        "tool_calls": tool_calls_count,
        "status": status,
        "finish_reason": finish_reason,
        "error": error,
        "timed_out": status == "timeout",
        "duration_ms": int((time.monotonic() - started) * 1000),
        "usage": usage,
        "transcript_path": str(transcript_path),
    }
