# Design notes

Docker is the execution boundary because coding tasks can use unrelated languages,
package managers, operating-system packages, and test frameworks. A bundle can
either build an environment from a pinned source commit or derive a baseline from
a published image. The initialized image is keyed by the canonical bundle hash,
and later commands record its immutable Docker image ID. Mutable upstream tags
therefore cannot silently change an already initialized task. A source build uses
a temporary context containing only the repository and generated Dockerfile, so
the golden patch and hidden tests never enter image layers.

Hidden evaluation is enforced structurally rather than through prompt wording.
Initialization resets the repository to the configured base commit and removes
remotes, future refs, reflogs, and unreachable objects. Before a workspace-reading
solver starts, the harness additionally removes `tests.hidden_paths`, creates an
orphan root commit from that redacted tree, and prunes the original commit. This
prevents recovery of pre-existing P2P source with `git show` while leaving ordinary
tests in non-hidden paths available. Whole-path hiding is framework-neutral but
requires selected tests to live in dedicated files or directories.

The solver receives a composed description outside the repository, but not
`task.json`, `patch.diff`, `test.patch`, hidden paths, or selected test IDs. Its
staged binary diff is anchored to the redacted root, so test removals are not part
of the candidate. That diff is the only state transferred into a fresh judge,
which applies it to the untouched configured baseline, applies the hidden test
patch, and runs without networking. Containers receive no host mounts, home
directories, credential stores, or Docker socket.

The LiteLLM controller runs on the host because provider credentials and network
access do not belong inside the untrusted workspace. It sends only the task
description and shell-tool results to the selected provider. The single shell tool
executes inside the same hardened, redacted container used by executable solvers.
Provider errors, turn limits, token usage, and workspace-network policy are
reported independently from the fresh-container evaluation result.

The runner deliberately uses one configurable command template per test ID. This
keeps the lifecycle independent of pytest, Go, Jest, Cargo, or another framework,
and makes the exit-code contract easy to understand. The tradeoff is performance:
large P2P lists can repeatedly start a test framework. Tests are sequential by
default to avoid order-dependent parallel failures. A future structured batch
runner could improve throughput without changing bundle, result, database, or
report semantics.

SQLite stores compact searchable metadata, while logs and patches remain ordinary
files. Every invocation gets a command ID and a `running` row before bundle parsing
or Docker work, then transitions to `completed` or `error`. Evaluation outcomes
are separate: `valid`/`invalid` and `resolved`/`unresolved` describe benchmark
behavior, while `error` means the harness could not produce a trustworthy verdict.
This distinction prevents an unapplied patch or missing test executable from being
mistaken for an ordinary failing candidate.

The isolation model is intentionally conservative but not a complete hostile-code
sandbox. Docker Desktop and the Docker daemon remain privileged host services;
kernel vulnerabilities and resource side channels are outside the current scope. Runtime
containers drop capabilities, enable `no-new-privileges`, cap CPU, memory and PID
usage, avoid host mounts, and default to no network. Some upstream images require
root or writable system paths, so forced non-root and read-only-root operation are
not universal. Networked solvers are explicitly opt-in and their reports record
the weaker contamination posture.
