from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import ResourceConfig


class DockerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def output(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr


def run_process(
    command: Sequence[str],
    *,
    timeout: int | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        result = CommandResult(
            command=tuple(command),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = CommandResult(
            command=tuple(command),
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise DockerError(f"command not found: {command[0]}") from exc
    if check and (result.timed_out or result.exit_code != 0):
        detail = result.output.strip() or "no output"
        raise DockerError(f"command failed: {shlex.join(command)}\n{detail}")
    return result


class DockerClient:
    def __init__(self, log_path: Path | None = None):
        self.log_path = log_path

    def _log(self, title: str, result: CommandResult, *, command: str | None = None) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = command or shlex.join(result.command)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {rendered}\n")
            if result.stdout:
                handle.write(result.stdout)
                if not result.stdout.endswith("\n"):
                    handle.write("\n")
            if result.stderr:
                handle.write(result.stderr)
                if not result.stderr.endswith("\n"):
                    handle.write("\n")
            handle.write(
                f"[{title}: exit={result.exit_code} timeout={result.timed_out} "
                f"duration_ms={result.duration_ms}]\n"
            )

    def command(
        self,
        args: Sequence[str],
        *,
        timeout: int | None = None,
        check: bool = False,
        log_command: str | None = None,
    ) -> CommandResult:
        result = run_process(["docker", *args], timeout=timeout, check=False)
        self._log("docker", result, command=log_command)
        if check and (result.timed_out or result.exit_code != 0):
            detail = result.output.strip() or "no output"
            raise DockerError(f"Docker command failed: {detail}")
        return result

    def require_daemon(self) -> None:
        result = self.command(["info", "--format", "{{.ServerVersion}}"], timeout=15)
        if result.exit_code != 0:
            raise DockerError(
                "Docker daemon is unavailable. Start Docker Desktop or the Docker service "
                "and retry."
            )

    def pull(self, image: str, platform: str | None = None) -> None:
        args = ["pull"]
        if platform:
            args += ["--platform", platform]
        args.append(image)
        self.command(args, timeout=3600, check=True)

    def inspect_image_id(self, image: str) -> str:
        result = self.command(["image", "inspect", image, "--format", "{{.Id}}"], check=True)
        return result.stdout.strip()

    def inspect_digest(self, image: str) -> str | None:
        result = self.command(
            ["image", "inspect", image, "--format", "{{join .RepoDigests \"\\n\"}}"]
        )
        values = [item for item in result.stdout.splitlines() if "@sha256:" in item]
        return values[0] if values else None

    def tag(self, source: str, target: str) -> None:
        self.command(["tag", source, target], check=True)

    def build(
        self,
        context: Path,
        tag: str,
        *,
        platform: str | None = None,
        timeout: int = 3600,
    ) -> str:
        args = ["build", "--progress", "plain", "--pull=false", "-t", tag]
        if platform:
            args += ["--platform", platform]
        args.append(str(context))
        self.command(args, timeout=timeout, check=True)
        return self.inspect_image_id(tag)

    def create_container(
        self,
        image: str,
        *,
        shell: str,
        workdir: str,
        resources: ResourceConfig,
        network: bool = False,
        environment: dict[str, str] | None = None,
        hardened: bool = True,
    ) -> str:
        args = ["create", "--workdir", workdir]
        if hardened:
            args += [
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(resources.pids_limit),
                "--memory",
                f"{resources.memory_mb}m",
                "--cpus",
                str(resources.cpus),
            ]
        if not network:
            args += ["--network", "none"]
        for name, value in sorted((environment or {}).items()):
            args += ["--env", f"{name}={value}"]
        args += [
            "--entrypoint",
            shell,
            image,
            "-c",
            "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
        ]
        # Do not log the concrete create command: it may contain allowlisted secrets.
        result = self.command(args, check=True, log_command="docker create [redacted]")
        return result.stdout.strip()

    def start(self, container: str) -> None:
        self.command(["start", container], check=True)

    def remove(self, container: str) -> None:
        self.command(["rm", "-f", container])

    def exec(
        self,
        container: str,
        shell: str,
        command: str,
        *,
        timeout: int | None = None,
        check: bool = False,
    ) -> CommandResult:
        result = self.command(
            ["exec", container, shell, "-c", command], timeout=timeout, check=False
        )
        if check and (result.timed_out or result.exit_code != 0):
            detail = result.output.strip() or "no output"
            raise DockerError(f"container command failed: {detail}")
        return result

    def mkdir(self, container: str, shell: str, path: str) -> None:
        self.exec(container, shell, f"mkdir -p {shlex.quote(path)}", check=True)

    def copy_in(self, source: Path, container: str, destination: str) -> None:
        self.command(["cp", str(source), f"{container}:{destination}"], check=True)

    def copy_out(self, container: str, source: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.command(["cp", f"{container}:{source}", str(destination)], check=True)


class Container:
    def __init__(
        self,
        docker: DockerClient,
        image: str,
        *,
        shell: str,
        workdir: str,
        resources: ResourceConfig,
        network: bool = False,
        environment: dict[str, str] | None = None,
    ):
        self.docker = docker
        self.image = image
        self.shell = shell
        self.workdir = workdir
        self.resources = resources
        self.network = network
        self.environment = environment
        self.id: str | None = None

    def __enter__(self) -> Container:
        self.id = self.docker.create_container(
            self.image,
            shell=self.shell,
            workdir=self.workdir,
            resources=self.resources,
            network=self.network,
            environment=self.environment,
        )
        self.docker.start(self.id)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.id:
            self.docker.remove(self.id)

    def exec(
        self, command: str, *, timeout: int | None = None, check: bool = False
    ) -> CommandResult:
        if not self.id:
            raise DockerError("container has not been started")
        return self.docker.exec(self.id, self.shell, command, timeout=timeout, check=check)

    def copy_in(self, source: Path, destination: str) -> None:
        if not self.id:
            raise DockerError("container has not been started")
        parent = str(Path(destination).parent)
        self.docker.mkdir(self.id, self.shell, parent)
        self.docker.copy_in(source, self.id, destination)

    def copy_out(self, source: str, destination: Path) -> None:
        if not self.id:
            raise DockerError("container has not been started")
        self.docker.copy_out(self.id, source, destination)


def allowed_environment(names: Sequence[str]) -> dict[str, str]:
    invalid = [name for name in names if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)]
    if invalid:
        raise DockerError(f"invalid environment variable names: {', '.join(invalid)}")
    missing = [name for name in names if name not in os.environ]
    if missing:
        raise DockerError(f"requested environment variables are not set: {', '.join(missing)}")
    return {name: os.environ[name] for name in names}
