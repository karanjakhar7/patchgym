# PatchGym

PatchGym packages, validates, and evaluates SWE-bench-style coding tasks
inside Docker. Solvers work in a redacted baseline container without the golden
patch, hidden test patch, selected test source paths, or selected test identifiers.
Their changes are exported as a Git patch and graded in a second clean container.

It is an open-source, standalone toolkit for building reproducible reinforcement
learning environments for coding agents: each bundle is an episode with an
isolated workspace, a hidden verifier, and a binary resolved/unresolved reward.

## Prerequisites and installation

- Python 3.11 or newer
- Git
- Docker with a running daemon
- `uv` (recommended)

```bash
uv sync --extra dev
uv run patchgym --help
```

Installing the project creates equivalent `task` and `patchgym` executables.
Runtime state defaults to `.patchgym/`; use the global `--state-dir` option or
`PATCHGYM_STATE_DIR` to put it elsewhere.

## Quick start

The tiny example contains an offline Git bundle, a one-line defect, a golden
patch, and a hidden regression test.

```bash
uv run patchgym init examples/tiny
uv run patchgym validate examples/tiny

# Negative control: exits 1 and writes an unresolved report.
uv run patchgym run examples/tiny --solver noop

# Positive control: exits 0 and writes a resolved report.
uv run patchgym run examples/tiny \
  --solver patch \
  --candidate-patch examples/tiny/patch.diff

uv run patchgym show <command-id>
uv run patchgym show <command-id> --json
```

The repository also includes `examples/swebench-pro-navidrome`, an exact public
SWE-bench Pro task backed by its published `linux/amd64` image. It uses the same
commands but requires a substantially larger image and more Docker resources.

`init` and successful `run` commands exit 0. A completed validation or evaluation
whose acceptance criteria are not met exits 1. Configuration, Docker, patch,
timeout, and other infrastructure errors exit 2.

## Running an executable solver

An executable solver runs inside the sanitized baseline container. It receives
only the repository, the composed task description, and files explicitly copied
with `--solver-file`.

```bash
uv run patchgym run examples/tiny \
  --solver exec \
  --solver-file examples/tiny/example_solver.py \
  --solver-command "python /solver/example_solver.py"
```

The following environment variables are always present:

- `TASK_WORKSPACE`: repository working directory
- `TASK_DESCRIPTION`: composed problem, requirements, and interface document

Network access is disabled by default. A networked agent may use
`--allow-network --pass-env API_KEY`; only explicitly named variables are passed,
and their values are redacted from command logs. Enabling network access weakens
benchmark contamination protections and is recorded in the report.

`patchgym run` normally requires a successful `patchgym validate` for the exact bundle
hash and image ID. `--skip-validation` is available for harness debugging.

## Running a LiteLLM solver

The built-in LLM solver uses LiteLLM on the host and exposes only a shell tool into
the isolated solver container. Provider credentials remain in the host environment
and are never copied into Docker or written to reports.

Credentials can also live in a `.env` file in the working directory (copy
`.env.example`). Values already set in the shell take precedence; `.env` is
gitignored.

```bash
export OPENAI_API_KEY="..."
uv run patchgym run examples/tiny --solver llm
```

The default is `openai/gpt-5.6-terra` with medium reasoning. Select any LiteLLM
model with native function calling:

```bash
# Anthropic
export ANTHROPIC_API_KEY="..."
uv run patchgym run examples/tiny \
  --solver llm \
  --model anthropic/<tool-capable-model>

# Local Ollama or another custom endpoint
uv run patchgym run examples/tiny \
  --solver llm \
  --model ollama/<tool-capable-model> \
  --api-base http://127.0.0.1:11434
```

Use `--max-turns`, `--solver-timeout`, and a less expensive model to control cost.
`--reasoning-effort` accepts `none`, `low`, `medium`, `high`, `xhigh`, or `max`;
LiteLLM drops it for providers that do not support it. Models without native tool
calling are rejected rather than driven through a fragile text-action protocol.
`--allow-network` controls the workspace container only—the host must still reach
the selected provider.

## Bundle format

Each bundle contains `task.json` and the files it references:

```text
my-task/
├── task.json
├── description.md
├── requirements.md       # optional
├── interface.md          # optional
├── patch.diff            # golden implementation patch
└── test.patch            # hidden evaluation tests
```

A build-backed manifest looks like this:

```json
{
  "schema_version": 1,
  "task_id": "widget-zero-case",
  "repository": {
    "url": "https://github.com/example/widgets.git",
    "base_commit": "0123456789abcdef0123456789abcdef01234567"
  },
  "description": {
    "problem": "description.md",
    "requirements": "requirements.md",
    "interface": "interface.md"
  },
  "patches": {
    "gold": "patch.diff",
    "tests": "test.patch"
  },
  "environment": {
    "kind": "build",
    "base_image": "python:3.11-bookworm",
    "setup_commands": ["pip install -e ."],
    "workdir": "/workspace",
    "shell": "/bin/sh"
  },
  "runner": {
    "command_template": "pytest -q {test_id}",
    "timeout_seconds": 300
  },
  "tests": {
    "fail_to_pass": ["tests/test_widget.py::test_zero"],
    "pass_to_pass": ["tests/test_widget.py::test_existing"],
    "hidden_paths": ["tests/test_widget.py"]
  }
}
```

For an existing benchmark image, replace the environment with:

```json
{
  "kind": "image",
  "image": "registry/example:published-tag",
  "workdir": "/app",
  "platform": "linux/amd64",
  "shell": "/bin/bash"
}
```

The base image must contain Git and the configured shell. Build setup may use the
network, but the resulting repository must have clean tracked files. `{test_id}`
is shell-quoted by the harness before substitution. F2P must contain at least one
test; P2P may be empty.

`tests.hidden_paths` lists repository-relative files or directories containing
selected evaluation tests. Every path modified by `test.patch` must be covered.
Before an `exec` or `llm` solver runs, these paths are removed, a new root commit is
created, and the original Git objects and reflogs are pruned. Hidden paths cannot
be absolute, escape the repository, contain `.git`, or use shell expansion.

Hiding operates on whole paths. Keep selected tests in dedicated files or
directories: if an ordinary test shares a hidden file, it is also unavailable to
the solver. Bundle authors must list files containing pre-existing P2P tests;
generic test identifiers cannot be mapped to source locations across every test
framework.

## Reports and command history

Each command is inserted into `.patchgym/patchgym.db` before work begins.
Large artifacts are written beneath `.patchgym/runs/<command-id>/`, including:

- `report.json`
- Docker, solver, and test logs
- `candidate.patch` for solver runs
- `solver-transcript.json` for LLM prompts, tool calls, and bounded tool output

The database keeps lifecycle status separate from evaluation outcome, so an
unresolved candidate is distinguishable from a harness error. `patchgym show` queries
the stored record without reproducing the run.

Sanitized reports from completed end-to-end runs are checked into `artifacts/`:
the tiny noop negative control and the resolved SWE-bench Pro golden-patch control.

## Development

```bash
uv run --extra dev ruff check .
uv run --extra dev pytest -m "not docker"
uv run --extra dev pytest -m docker
```

Docker tests skip automatically when the daemon is unavailable. The published
SWE-bench Pro example is an `linux/amd64` image and may run under emulation on
Apple Silicon.

## Troubleshooting

- **Docker daemon unavailable:** start Docker Desktop or the Docker service.
- **Bundle changed after init:** rerun `patchgym init BUNDLE --force`.
- **No matching validation:** run `patchgym validate BUNDLE`, or use
  `--skip-validation` only while debugging the harness.
- **Patch does not apply:** inspect the run's `docker.log` and confirm the patch
  was produced against the configured base commit.
- **Exit 1 from noop:** this is the expected negative-control outcome.
- **Image architecture mismatch:** set `environment.platform`, normally
  `linux/amd64` for published SWE-bench Pro images.
- **LiteLLM model rejected:** choose a model for which LiteLLM reports native
  function-calling support.
- **Provider authentication failed:** set the provider's standard environment
  variable, such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`; do not use
  `--pass-env` with the built-in LLM solver.
