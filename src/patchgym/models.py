from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .util import COMMIT_RE, TASK_ID_RE


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryConfig(StrictModel):
    url: str = Field(min_length=1)
    base_commit: str

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("repository URL cannot contain control characters")
        if value.startswith("file://"):
            raise ValueError("file:// repository URLs are not allowed; use a bundle-relative path")
        return value

    @field_validator("base_commit")
    @classmethod
    def full_sha(cls, value: str) -> str:
        if not COMMIT_RE.fullmatch(value):
            raise ValueError("base_commit must be a full 40-character hexadecimal commit")
        return value.lower()


class DescriptionConfig(StrictModel):
    problem: str = "description.md"
    requirements: str | None = None
    interface: str | None = None


class PatchConfig(StrictModel):
    gold: str = "patch.diff"
    tests: str = "test.patch"


class ResourceConfig(StrictModel):
    cpus: float = Field(default=2.0, gt=0)
    memory_mb: int = Field(default=4096, ge=128)
    pids_limit: int = Field(default=512, ge=16)


class BuildEnvironment(StrictModel):
    kind: Literal["build"]
    base_image: str = Field(min_length=1)
    setup_commands: list[str] = Field(default_factory=list)
    workdir: str = "/workspace"
    platform: str | None = None
    shell: str = "/bin/sh"

    @field_validator("base_image", "platform")
    @classmethod
    def safe_docker_value(cls, value: str | None) -> str | None:
        if value is not None and (not value or re.search(r"\s", value)):
            raise ValueError("Docker image and platform values cannot contain whitespace")
        return value

    @field_validator("workdir", "shell")
    @classmethod
    def absolute_container_path(cls, value: str) -> str:
        if not value.startswith("/") or re.search(r"[\s\x00]", value):
            raise ValueError("container paths must be absolute and cannot contain whitespace")
        return value

    @field_validator("setup_commands")
    @classmethod
    def safe_setup_commands(cls, value: list[str]) -> list[str]:
        if any(not command.strip() or "\x00" in command for command in value):
            raise ValueError("setup commands cannot be blank or contain NUL bytes")
        return value


class ImageEnvironment(StrictModel):
    kind: Literal["image"]
    image: str = Field(min_length=1)
    workdir: str = "/app"
    platform: str | None = None
    shell: str = "/bin/sh"

    @field_validator("image", "platform")
    @classmethod
    def safe_docker_value(cls, value: str | None) -> str | None:
        if value is not None and (not value or re.search(r"\s", value)):
            raise ValueError("Docker image and platform values cannot contain whitespace")
        return value

    @field_validator("workdir", "shell")
    @classmethod
    def absolute_container_path(cls, value: str) -> str:
        if not value.startswith("/") or re.search(r"[\s\x00]", value):
            raise ValueError("container paths must be absolute and cannot contain whitespace")
        return value


EnvironmentConfig = Annotated[BuildEnvironment | ImageEnvironment, Field(discriminator="kind")]


class RunnerConfig(StrictModel):
    command_template: str
    timeout_seconds: int = Field(default=300, ge=1)

    @field_validator("command_template")
    @classmethod
    def one_placeholder(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("command_template cannot be blank or contain NUL bytes")
        if value.count("{test_id}") != 1:
            raise ValueError("command_template must contain {test_id} exactly once")
        return value


class TestConfig(StrictModel):
    fail_to_pass: list[str] = Field(min_length=1)
    pass_to_pass: list[str] = Field(default_factory=list)
    hidden_paths: list[str] = Field(default_factory=list)

    @field_validator("hidden_paths")
    @classmethod
    def safe_hidden_paths(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        expansion_chars = set("*?[]{}$`\\")
        for item in value:
            if not item or item != item.strip() or "\x00" in item:
                raise ValueError("hidden paths cannot be blank or contain surrounding whitespace")
            path = PurePosixPath(item)
            if path.is_absolute() or item in {".", ".."}:
                raise ValueError("hidden paths must be repository-relative paths")
            if any(part in {"", ".", "..", ".git"} for part in path.parts):
                raise ValueError("hidden paths cannot contain '.', '..', or '.git' components")
            if any(char in expansion_chars for char in item):
                raise ValueError("hidden paths cannot contain shell expansion characters")
            rendered = path.as_posix()
            if rendered != item:
                raise ValueError("hidden paths must be normalized POSIX paths")
            normalized.append(rendered)
        if len(set(normalized)) != len(normalized):
            raise ValueError("hidden paths must be unique")
        return normalized

    @model_validator(mode="after")
    def unique_tests(self) -> TestConfig:
        f2p = self.fail_to_pass
        p2p = self.pass_to_pass
        if len(set(f2p)) != len(f2p) or len(set(p2p)) != len(p2p):
            raise ValueError("test identifiers must be unique within each group")
        overlap = set(f2p) & set(p2p)
        if overlap:
            raise ValueError(f"test identifiers cannot be in both groups: {sorted(overlap)!r}")
        if any(not item.strip() for item in [*f2p, *p2p]):
            raise ValueError("test identifiers cannot be blank")
        return self


class TaskSpec(StrictModel):
    schema_version: Literal[1]
    task_id: str
    repository: RepositoryConfig
    description: DescriptionConfig = Field(default_factory=DescriptionConfig)
    patches: PatchConfig = Field(default_factory=PatchConfig)
    environment: EnvironmentConfig
    runner: RunnerConfig
    tests: TestConfig
    resources: ResourceConfig = Field(default_factory=ResourceConfig)

    @field_validator("task_id")
    @classmethod
    def safe_task_id(cls, value: str) -> str:
        if not TASK_ID_RE.fullmatch(value):
            raise ValueError("task_id must use only letters, digits, '.', '_', and '-'")
        return value

    def configuration_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class TestResult(StrictModel):
    test_id: str
    group: Literal["fail_to_pass", "pass_to_pass"]
    phase: Literal["baseline", "gold", "candidate"]
    repetition: int = 1
    status: TestStatus
    exit_code: int | None = None
    duration_ms: int
    log_path: str


class InitState(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str
    bundle_hash: str
    base_commit: str
    image_id: str
    image_ref: str
    source_image: str
    source_digest: str | None = None
    platform: str | None = None
    initialized_at: str


class BundleFiles(StrictModel):
    root: Path
    manifest: Path
    problem: Path
    requirements: Path | None
    interface: Path | None
    gold_patch: Path
    test_patch: Path
