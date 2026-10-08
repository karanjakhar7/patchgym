from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from patchgym.cli import app
from patchgym.db import Database

runner = CliRunner()


def test_invalid_bundle_is_recorded(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = runner.invoke(app, ["--state-dir", str(state), "init", str(tmp_path / "missing")])
    assert result.exit_code == 2
    records = Database(state).list_commands()
    assert len(records) == 1
    assert records[0]["status"] == "error"


def test_show_missing_command_is_recorded(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = runner.invoke(app, ["--state-dir", str(state), "show", "does-not-exist"])
    assert result.exit_code == 2
    records = Database(state).list_commands()
    assert records[0]["kind"] == "show"
    assert records[0]["target_command_id"] == "does-not-exist"


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    from patchgym.util import load_dotenv

    env = tmp_path / ".env"
    env.write_text('# c\nTB_A="one"\nexport TB_B=two\nTB_C=x\n')
    monkeypatch.setenv("TB_C", "shell")
    monkeypatch.delenv("TB_A", raising=False)
    monkeypatch.delenv("TB_B", raising=False)
    load_dotenv(env)
    import os

    assert (os.environ["TB_A"], os.environ["TB_B"], os.environ["TB_C"]) == ("one", "two", "shell")
    monkeypatch.delenv("TB_A")
    monkeypatch.delenv("TB_B")
