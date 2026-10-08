# AGENTS.md

## Project overview

This repository implements a Python 3.11 CLI for building, validating, and
evaluating SWE-bench-style coding tasks in Docker. The installed `task` and
`patchgym` commands are aliases for the same Typer application.

Read `README.md` for the public interface and `DESIGN.md` before changing the
execution or isolation model.

## Development commands

Use the workspace-local uv cache so commands work in restricted environments:

```bash
uv --cache-dir .uv-cache run --extra dev ruff check .
uv --cache-dir .uv-cache run --extra dev pytest -m "not docker"
uv --cache-dir .uv-cache run --extra dev pytest -m docker
uv --cache-dir .uv-cache build
```

Docker-marked tests require a running Docker daemon and may require permission to
access its socket. Do not silently replace them with mocks when changing container
lifecycle behavior.

## Code map

- `src/patchgym/models.py`: strict bundle, state, and result schemas.
- `src/patchgym/bundle.py`: manifest loading, path validation, and description
  composition.
- `src/patchgym/docker.py`: subprocess-based Docker boundary and resource limits.
- `src/patchgym/engine.py`: image initialization, validation, solvers, patch
  capture, and fresh-container judging.
- `src/patchgym/db.py` and `state.py`: command history and initialized image state.
- `src/patchgym/cli.py`: Typer commands, output, and exit-code policy.
- `examples/tiny`: offline end-to-end fixture backed by a Git bundle.
- `examples/swebench-pro-navidrome`: public real-world benchmark fixture.

## Required invariants

- Never expose `patch.diff`, `test.patch`, selected F2P/P2P identifiers, the bundle
  manifest, or command database to the solver container.
- Solving and judging must use different fresh containers. Transfer only the
  captured candidate patch between them.
- Judge containers always run without network access. Solver networking remains an
  explicit, reported opt-in.
- Do not mount host directories or the Docker socket into runtime containers. Use
  `docker cp` for explicit file transfer.
- Keep patch/noop judging anchored to the configured base commit. Workspace-reading
  solvers capture against their redacted root so hidden-path deletions are not
  exported, while retaining solver changes made in commits.
- Preserve the distinction between an unresolved candidate (exit 1) and an
  infrastructure/evaluation error (exit 2).
- Insert the database command record before substantive work and finalize failures
  so every attempted lifecycle command remains queryable.
- Treat bundle paths and Dockerfile-derived values as untrusted input. Preserve path
  containment, full-commit, shell-quoting, and control-character validation.
- Build contexts must contain only the fetched repository and generated Dockerfile;
  hidden bundle artifacts must never enter image layers.
- Keep initialized image IDs immutable for existing state. Mutable source tags are
  refreshed only through an explicit forced initialization.

## Testing expectations

Add focused unit tests for schema, database, reporting, and command-construction
changes. Run the Docker integration test for any modification involving images,
containers, patches, solvers, tests, Git sanitization, or isolation.

The Docker integration lifecycle must continue to demonstrate:

1. exact-commit initialization and cache reuse;
2. valid baseline and golden transitions;
3. noop unresolved and patch/exec resolved outcomes;
4. capture of solver changes even after a solver commit; and
5. absence of hidden files, remotes, and unreachable future Git objects.

Do not edit the checked-in SWE-bench Pro patch or task text except when deliberately
refreshing it from the official dataset; record the source and date in its
`ORIGIN.md`. Runtime output belongs under `.patchgym/` and must remain untracked.
