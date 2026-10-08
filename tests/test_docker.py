from __future__ import annotations

from pathlib import Path

import pytest

from patchgym.docker import CommandResult, DockerClient, DockerError, allowed_environment
from patchgym.models import ResourceConfig


class RecordingDocker(DockerClient):
    def __init__(self) -> None:
        super().__init__(Path("unused"))
        self.calls: list[tuple[list[str], str | None]] = []

    def command(self, args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((list(args), kwargs.get("log_command")))
        return CommandResult(tuple(["docker", *args]), 0, "container-id\n", "", 1)


def test_hardened_container_command_and_secret_redaction() -> None:
    docker = RecordingDocker()
    identifier = docker.create_container(
        "image-id",
        shell="/bin/sh",
        workdir="/workspace",
        resources=ResourceConfig(cpus=1, memory_mb=256, pids_limit=64),
        environment={"API_TOKEN": "secret"},
    )
    args, display = docker.calls[0]
    assert identifier == "container-id"
    assert args[args.index("--network") : args.index("--network") + 2] == ["--network", "none"]
    assert "ALL" in args
    assert "no-new-privileges" in args
    assert "API_TOKEN=secret" in args
    assert display == "docker create [redacted]"


def test_environment_allowlist_validates_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFE_TOKEN", "secret")
    assert allowed_environment(["SAFE_TOKEN"]) == {"SAFE_TOKEN": "secret"}
    with pytest.raises(DockerError, match="invalid environment"):
        allowed_environment(["BAD=NAME"])
    with pytest.raises(DockerError, match="not set"):
        allowed_environment(["MISSING_TOKEN"])
