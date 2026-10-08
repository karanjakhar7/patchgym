from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .models import InitState, TaskSpec
from .util import atomic_write_json


class StateError(RuntimeError):
    pass


def state_path(state_dir: Path, task_id: str) -> Path:
    return state_dir / "tasks" / task_id / "state.json"


def save_state(state_dir: Path, state: InitState) -> Path:
    path = state_path(state_dir, state.task_id)
    atomic_write_json(path, state.model_dump(mode="json"))
    return path


def load_state(state_dir: Path, bundle: TaskSpec) -> InitState:
    path = state_path(state_dir, bundle.task_id)
    if not path.is_file():
        raise StateError(f"task is not initialized; run 'task init <bundle>' first ({path})")
    try:
        state = InitState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise StateError(f"invalid initialization state at {path}: {exc}") from exc
    current_hash = bundle.configuration_hash()
    if state.bundle_hash != current_hash:
        raise StateError(
            "bundle configuration changed after init; run 'task init <bundle> --force'"
        )
    if state.base_commit != bundle.repository.base_commit:
        raise StateError("initialized base commit does not match task.json")
    return state
