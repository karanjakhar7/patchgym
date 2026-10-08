from __future__ import annotations

from pathlib import Path

from patchgym.db import Database
from patchgym.models import TestResult as ResultModel
from patchgym.models import TestStatus as Status


def test_command_and_test_result_round_trip(tmp_path: Path) -> None:
    database = Database(tmp_path / "state")
    artifacts = tmp_path / "state" / "runs" / "validate_1"
    database.start_command("validate_1", "validate", artifacts, task_id="tiny")
    database.add_test_result(
        "validate_1",
        ResultModel(
            test_id="tests.test_bug",
            group="fail_to_pass",
            phase="baseline",
            status=Status.FAILED,
            exit_code=1,
            duration_ms=12,
            log_path="test.log",
        ),
    )
    database.finish_command(
        "validate_1",
        status="completed",
        outcome="valid",
        exit_code=0,
        summary={"valid": True},
    )

    record = database.get_command("validate_1")

    assert record is not None
    assert record["summary"] == {"valid": True}
    assert record["tests"][0]["status"] == "failed"


def test_failed_command_remains_queryable(tmp_path: Path) -> None:
    database = Database(tmp_path / "state")
    database.start_command("init_1", "init", tmp_path / "artifacts")
    database.finish_command(
        "init_1", status="error", outcome=None, exit_code=2, error="Docker stopped"
    )
    record = database.get_command("init_1")
    assert record is not None
    assert record["status"] == "error"
    assert record["error"] == "Docker stopped"


def test_latest_validation_matches_bundle_and_image(tmp_path: Path) -> None:
    database = Database(tmp_path / "state")
    database.start_command(
        "validate_1",
        "validate",
        tmp_path / "artifacts",
        task_id="tiny",
        bundle_hash="bundle",
        image_id="image",
    )
    database.finish_command(
        "validate_1", status="completed", outcome="valid", exit_code=0
    )
    assert database.latest_valid_validation("tiny", "bundle", "image") is not None
    assert database.latest_valid_validation("tiny", "changed", "image") is None
