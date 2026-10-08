from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import TestResult
from .util import utc_now

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    error TEXT,
    bundle_hash TEXT,
    image_id TEXT,
    summary_json TEXT,
    artifact_dir TEXT NOT NULL,
    target_command_id TEXT
);

CREATE TABLE IF NOT EXISTS test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL REFERENCES commands(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    repetition INTEGER NOT NULL,
    test_group TEXT NOT NULL,
    test_id TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    duration_ms INTEGER NOT NULL,
    log_path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commands_task_kind
ON commands(task_id, kind, finished_at);

CREATE INDEX IF NOT EXISTS idx_test_results_command
ON test_results(command_id);
"""


class Database:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.expanduser().resolve()
        self.path = self.state_dir / "patchgym.db"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def start_command(
        self,
        command_id: str,
        kind: str,
        artifact_dir: Path,
        *,
        task_id: str | None = None,
        bundle_hash: str | None = None,
        image_id: str | None = None,
        target_command_id: str | None = None,
    ) -> None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO commands(
                    id, task_id, kind, status, started_at, bundle_hash,
                    image_id, artifact_dir, target_command_id
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    task_id,
                    kind,
                    utc_now(),
                    bundle_hash,
                    image_id,
                    str(artifact_dir),
                    target_command_id,
                ),
            )

    def attach_bundle(
        self, command_id: str, task_id: str, bundle_hash: str, image_id: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE commands SET task_id = ?, bundle_hash = ?, image_id = ? WHERE id = ?",
                (task_id, bundle_hash, image_id, command_id),
            )

    def finish_command(
        self,
        command_id: str,
        *,
        status: str,
        outcome: str | None,
        exit_code: int,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE commands
                SET status = ?, outcome = ?, finished_at = ?, exit_code = ?,
                    summary_json = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    outcome,
                    utc_now(),
                    exit_code,
                    json.dumps(summary, sort_keys=True) if summary is not None else None,
                    error,
                    command_id,
                ),
            )

    def add_test_result(self, command_id: str, result: TestResult) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO test_results(
                    command_id, phase, repetition, test_group, test_id,
                    status, exit_code, duration_ms, log_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    result.phase,
                    result.repetition,
                    result.group,
                    result.test_id,
                    result.status.value,
                    result.exit_code,
                    result.duration_ms,
                    result.log_path,
                ),
            )

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if result["summary_json"]:
                result["summary"] = json.loads(result.pop("summary_json"))
            else:
                result.pop("summary_json")
                result["summary"] = None
            tests = connection.execute(
                """
                SELECT phase, repetition, test_group, test_id, status,
                       exit_code, duration_ms, log_path
                FROM test_results WHERE command_id = ? ORDER BY id
                """,
                (command_id,),
            ).fetchall()
            result["tests"] = [dict(test) for test in tests]
            return result

    def latest_valid_validation(
        self, task_id: str, bundle_hash: str, image_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM commands
                WHERE task_id = ? AND kind = 'validate' AND status = 'completed'
                  AND outcome = 'valid' AND bundle_hash = ? AND image_id = ?
                ORDER BY finished_at DESC LIMIT 1
                """,
                (task_id, bundle_hash, image_id),
            ).fetchone()
            return dict(row) if row else None

    def list_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM commands ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
