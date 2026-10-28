# `pydantic-ai-opensandbox` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone package whose `OpenSandbox` capability runs `Coder`/`Shell`/`FileSystem` in an OpenSandbox sandbox, with the same constructor and behavior as the harness's `E2BSandbox`.

**Architecture:** A port of `pydantic_ai_harness/e2b_sandbox/` (`_capability.py` + `_backend.py`) onto the async `opensandbox` SDK. Tests mirror `tests/harness/e2b_sandbox/`: a host-backed fake SDK carries the conformance suite and 100% coverage offline; an opt-in live tier runs the same suite against a local `opensandbox-server`.

**Tech Stack:** Python ≥3.11, `uv`, `pydantic-ai-slim` (`pydantic_ai.workspaces`), `opensandbox` (async SDK), pytest + anyio, pyright strict, ruff.

**Spec:** `docs/superpowers/specs/2026-10-28-opensandbox-design.md`

**Reference code (read before each task, port its patterns):**
- E2B capability/backend: `/Users/ilayu/Documents/open_source/pydantic-ai/src/pydantic_ai_harness/pydantic_ai_harness/e2b_sandbox/`
- E2B tests and fake: `/Users/ilayu/Documents/open_source/pydantic-ai/tests/harness/e2b_sandbox/`
- Harness helpers: `/Users/ilayu/Documents/open_source/pydantic-ai/src/pydantic_ai_harness/pydantic_ai_harness/_workspace_provider.py`
- Conformance suite: `/Users/ilayu/Documents/open_source/pydantic-ai/pydantic_ai_slim/pydantic_ai/workspaces/conformance.py`
- OpenSandbox SDK: `/Users/ilayu/Documents/open_source/OpenSandbox/sdks/sandbox/python/src/opensandbox/` (`sandbox.py`, `services/command.py`, `services/filesystem.py`, `exceptions/sandbox.py`, `models/`)

## Global Constraints

- Package `pydantic-ai-opensandbox`, import name `pydantic_ai_opensandbox`, repo `/Users/ilayu/Documents/open_source/pydantic-ai-opensandbox`, `src/` layout.
- `requires-python = ">=3.11"`; depend on `pydantic-ai-slim>=2.63.0` and `opensandbox>=1.2.0,<2`.
- Code style identical to pydantic-ai: ruff line length 120, single quotes, pyright strict, `from __future__ import annotations`, docstrings in the harness voice. Copy the relevant `[tool.ruff]` / `[tool.pyright]` settings from pydantic-ai's root `pyproject.toml`.
- Do not import private harness modules (`pydantic_ai_harness._*`). Copy the helpers you need into `src/pydantic_ai_opensandbox/_workspace_provider.py` with a module docstring naming the source file and commit it was copied from.
- `WorkspaceRef(provider='opensandbox', id=<sandbox id>)`.
- `DEFAULT_SANDBOX_TIMEOUT = 3_600`.
- Connection comes only from the SDK's env vars `OPEN_SANDBOX_DOMAIN` and `OPEN_SANDBOX_API_KEY`; no connection fields.
- 100% line and branch coverage from the offline tier alone; live tests use `# pragma: lax no cover` / `pragma: no cover - live` like the harness.
- Commits authored as the user; no publishing, no remote, no PyPI in this plan.

## Review Focus

1. **Two concurrent first operations** on a fresh backend must create exactly one sandbox (E2B's detached acquisition task + lock). Test: `test_concurrent_first_use_creates_one_sandbox` in Task 3.
2. **Server unreachable or wrong `OPEN_SANDBOX_DOMAIN`** must raise `WorkspaceUnavailableError` naming the env var, not a raw `httpx`/SDK exception. Test: `test_unreachable_server_is_unavailable` in Task 3.
3. **A command that times out or is cancelled** must actually be stopped in the sandbox (not just abandoned client-side), and `WorkspaceTimeoutError` carries output so far. Tests: `test_timeout_kills_command`, `test_cancel_kills_command` in Task 4.
4. **Binary / non-UTF-8 file contents** round-trip byte-for-byte through `write_bytes`/`read_bytes`. Test: `test_binary_round_trip` in Task 5.
5. **Attaching to a deleted sandbox** raises `WorkspaceUnavailableError` and never silently creates a replacement. Test: `test_attach_to_deleted_sandbox_is_unavailable` in Task 3.

---

### Task 1: Scaffold the package and probe the real SDK

Everything later depends on how the real server behaves, so this task establishes the tooling and records verified SDK facts.

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.python-version` (3.12), `README.md` (placeholder heading only), `src/pydantic_ai_opensandbox/__init__.py`, `src/pydantic_ai_opensandbox/py.typed`, `src/pydantic_ai_opensandbox/_workspace_provider.py`, `tests/__init__.py`, `tests/conftest.py`, `scripts/probe_sdk.py`, `docs/sdk-notes.md`

**Interfaces:**
- Produces: `_workspace_provider` with the same names and signatures as the harness file (`safe_credential_reason`, `running_on_asyncio`, `stop_shielded`, `command_deadline`, `run_to_completion`, `absolute_path`, `command_argv`, `check_env_types`, `check_env`, `check_working_dir`, `check_integer`, `check_timeout`), copying only those later tasks use.
- Produces: `docs/sdk-notes.md`, the verified facts Tasks 2–5 rely on, and the chosen `DEFAULT_IMAGE`.
- Produces: pytest marker `opensandbox_live`, gated in `tests/conftest.py` by `PYDANTIC_AI_OPENSANDBOX_LIVE=1` and a set `OPEN_SANDBOX_DOMAIN` (fail instead of skip under `OPENSANDBOX_REQUIRE_LIVE=1`), ported from the E2B `pytest_runtest_setup`.

- [ ] **Step 1:** Write `pyproject.toml` (hatchling, deps per Global Constraints, dev group: `pytest`, `anyio`, `pyright`, `ruff`, `coverage[toml]`, `inline-snapshot`, `pydantic-ai-harness` for the e2e test, `pydantic-ai-slim[anthropic]`), plus ruff/pyright/coverage config. Run `uv sync`. Expected: resolves.
- [ ] **Step 2:** Start a local server: `uvx opensandbox-server init-config ~/.sandbox.toml --example docker` then `uvx opensandbox-server` in the background. Export `OPEN_SANDBOX_DOMAIN=localhost:8080`. Expected: `curl localhost:8080/health` (or the documented health path) succeeds.
- [ ] **Step 3:** Write `scripts/probe_sdk.py` that, against the live server, checks and prints each of: create with an image, `timeout`, `env`; `connect` by id; `renew`; `kill`; `connect` to a killed id (exception type and message); `get_info` state for a paused sandbox and `resume`; `commands.run` exit code, stdout/stderr separation, `working_directory`, per-command env (does `RunCommandOpts` have an env field? if not, how to pass env: `env K=V ...` prefix); server `timeout` behavior (process killed? output returned?); `interrupt(execution_id)` killing the process group or only the leader; whether `setsid` exists in the image; `files.read_bytes` / `write_file` with binary data; `get_file_info` / `list_directory` on missing paths (exception type), symlinks, and directories; `delete_files` vs `delete_directories`; `NetworkPolicy(default_action='deny')` actually blocking `curl https://example.com`; the exception raised with a wrong domain and with a wrong API key.
- [ ] **Step 4:** Pick `DEFAULT_IMAGE`: an image with `sh`, Python 3, git, and ideally ripgrep that the probe showed starting cleanly (try OpenSandbox's code-interpreter image first; fall back to a `python:3.12-slim`-based one). Record it and every probe result in `docs/sdk-notes.md`, with the SDK version and date, in the style of the E2B backend's module docstring.
- [ ] **Step 5:** Copy the needed helpers into `_workspace_provider.py`; port the live gate into `tests/conftest.py`. Run `uv run ruff check . && uv run pyright && uv run pytest`. Expected: lint and types clean, pytest collects 0 tests without error.
- [ ] **Step 6:** Commit: `git commit -m "chore: scaffold package and record verified OpenSandbox SDK behavior"`.

### Task 2: Host-backed fake OpenSandbox SDK

**Files:**
- Create: `tests/fake_opensandbox.py`, extend `tests/conftest.py`

**Interfaces:**
- Consumes: `docs/sdk-notes.md` (the fake must reproduce those behaviors).
- Produces: `class FakeOpenSandbox` with `.module` (a stand-in for the `opensandbox` module exposing `Sandbox` with the classmethods/services the backend uses), `.host_root: Path`, `.close()`, and knobs tests use to inject failures: `fail_next(operation: str, error: Exception)`, `sandboxes: dict[str, FakeSandboxState]` (to delete/pause one), `created: int`. Fixture `fake_opensandbox` monkeypatches `pydantic_ai_opensandbox._backend.opensandbox`; autouse `_no_real_opensandbox` poisons it for non-live tests (ported from E2B's `_PoisonedE2B`).

- [ ] **Step 1:** Port `fake_e2b.py`'s design: real exception and model classes imported from `opensandbox`, every await suspends, commands run on the host under `tmp_path` via `subprocess` in their own process group so interrupt/timeout really kill them, file ops act on the host directory.
- [ ] **Step 2:** Add a sanity test `tests/test_fake.py::test_fake_runs_command_and_files` creating a fake sandbox, running `echo hi`, writing and reading a file. Run `uv run pytest tests/test_fake.py`. Expected: PASS.
- [ ] **Step 3:** Commit: `git commit -m "test: add host-backed fake OpenSandbox SDK"`.

### Task 3: Backend acquisition, lifecycle and error translation

**Files:**
- Create: `src/pydantic_ai_opensandbox/_backend.py`
- Test: `tests/test_backend.py`

**Interfaces:**
- Produces:
  ```python
  DEFAULT_SANDBOX_TIMEOUT = 3_600
  DEFAULT_IMAGE: str  # from docs/sdk-notes.md
  class OpenSandboxBackend(WorkspaceBackend, SupportsCommands, SupportsFilesystem):
      def __init__(self, *, sandbox: opensandbox.Sandbox | None = None, ref: WorkspaceRef | None = None,
                   image: str = DEFAULT_IMAGE, allow_internet_access: bool = True,
                   sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT, working_dir: str | None = None,
                   env: Mapping[str, str] | None = None) -> None
      async def get_sandbox(self) -> opensandbox.Sandbox
      @property
      def ref(self) -> WorkspaceRef | None
      async def working_dir(self) -> str
  async def kill_sandbox(sandbox_id: str) -> None
  ```
  Same `__init__` validation and `ValueError` messages as `E2BSandboxBackend`, with `'opensandbox'` for the provider. Module docstring records SDK assumptions (from `docs/sdk-notes.md`) like E2B's. `ImportError` message: `'Install `pydantic-ai-opensandbox` with the `opensandbox` SDK to use OpenSandbox.'` only if the SDK import fails.

- [ ] **Step 1: Write the failing tests** (all with `fake_opensandbox`):
  - `test_construction_does_no_io` – `OpenSandboxBackend()` → `fake.created == 0`, `backend.ref is None`.
  - `test_first_use_creates_sandbox` – after `await backend.working_dir()`: `fake.created == 1`, `backend.ref == WorkspaceRef(provider='opensandbox', id=<fake id>)`, create received `image`, `env`, `timeout == timedelta(seconds=3600)`.
  - `test_concurrent_first_use_creates_one_sandbox` – `anyio` task group running 5 `working_dir()` calls → `fake.created == 1`.
  - `test_working_dir_created_on_new_sandbox` – `working_dir='/w/project'` (under host root mapping) → directory exists, `working_dir()` returns it.
  - `test_attach_renews_lifetime` – `OpenSandboxBackend(ref=existing_ref)` first use → `connect` called, `renew(timedelta(seconds=3600))` called, `fake.created == 0`.
  - `test_attach_resumes_paused_sandbox` – fake marks sandbox paused → attach calls `resume`.
  - `test_attach_to_deleted_sandbox_is_unavailable` – ref to a deleted sandbox → `WorkspaceUnavailableError`, `fake.created == 0`.
  - `test_unreachable_server_is_unavailable` – fake raises the connection exception from the probe → `WorkspaceUnavailableError` whose message mentions `OPEN_SANDBOX_DOMAIN`.
  - `test_auth_failure_is_unavailable` – the probe's auth exception → `WorkspaceUnavailableError` mentioning `OPEN_SANDBOX_API_KEY`, credentials not echoed.
  - `test_image_refused_is_unavailable` – create refused for the image → `WorkspaceUnavailableError` naming the image.
  - `test_no_internet_sends_deny_policy` – `allow_internet_access=False` → create got `NetworkPolicy(default_action='deny')`; `True` → `network_policy is None`.
  - `test_kill_sandbox` – `kill_sandbox(id)` kills without connecting a workspace; unknown id raises nothing or `WorkspaceUnavailableError` per sdk-notes (pick and assert).
  - Port the remaining `__init__`-validation tests from `tests/harness/e2b_sandbox/test_backend.py` (provider mismatch, `sandbox`+`ref`, bad `sandbox_timeout`, non-string env).
- [ ] **Step 2:** Run `uv run pytest tests/test_backend.py`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement by porting E2B's `get_sandbox`/`_acquire`/`_create`/`_attach`/`_sdk_errors`/`_translate`/`kill_sandbox`, swapping SDK calls per sdk-notes. Keep the asyncio detached-acquisition task and the Trio `UserError` if the SDK is asyncio-only (check sdk-notes).
- [ ] **Step 4:** Run `uv run pytest tests/test_backend.py && uv run pyright`. Expected: PASS, 0 errors.
- [ ] **Step 5:** Commit: `git commit -m "feat: OpenSandboxBackend acquisition and error translation"`.

### Task 4: Commands

**Files:**
- Modify: `src/pydantic_ai_opensandbox/_backend.py`
- Test: `tests/test_backend.py`

**Interfaces:**
- Produces: `async def run(self, command: WorkspaceCommand, *, shell: bool = False, env: Mapping[str, str] | None = None, timeout: float | None = None) -> CommandResult` on `OpenSandboxBackend`.

- [ ] **Step 1: Write the failing tests:**
  - `test_run_argv_and_exit_code` – `run(['sh', '-c', 'echo out; echo err >&2; exit 3'])` → `CommandResult` with exit code 3 and stdout/stderr as the contract defines (check `CommandResult` fields).
  - `test_run_shell_string` – `run('echo $((1+1))', shell=True)` → output `'2\n'`.
  - `test_run_env_layers_on_backend_env` – backend `env={'A': '1', 'B': '1'}`, `run(..., env={'B': '2'})` → sees `A=1 B=2`.
  - `test_run_uses_working_dir` – `pwd -P` output equals `await backend.working_dir()`.
  - `test_timeout_kills_command` – `run(['sh', '-c', 'echo started; sleep 30'], timeout=0.5)` raises `WorkspaceTimeoutError` containing `'started'`; the fake reports no live process afterwards.
  - `test_cancel_kills_command` – cancel a `sleep 30` run via `anyio.move_on_after` → the fake reports no live process.
  - `test_output_limit` – port E2B's `WorkspaceOutputLimitError` test.
  - `test_sandbox_gone_mid_command_is_unavailable` – fake deletes the sandbox during the command → `WorkspaceUnavailableError`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement by porting E2B's `run` (client-side deadline, `stop_shielded`, `setsid` probing, output counting), using `commands.run` with streaming `handlers` and `interrupt(execution_id)` per sdk-notes. If the server's own `timeout` kills the process reliably, still enforce the deadline client-side like E2B so partial output is kept.
- [ ] **Step 4:** Run `uv run pytest tests/test_backend.py && uv run pyright`. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: run commands in OpenSandbox"`.

### Task 5: Filesystem

**Files:**
- Modify: `src/pydantic_ai_opensandbox/_backend.py`
- Test: `tests/test_backend.py`

**Interfaces:**
- Produces on `OpenSandboxBackend`: `read_bytes(path) -> bytes`, `write_bytes(path, data) -> None`, `stat(path) -> FileEntry`, `list_dir(path) -> Sequence[FileEntry]`, `make_dir(path) -> None`, `remove(path) -> None`, `exists(path) -> bool`, all with E2B's exact semantics.

- [ ] **Step 1: Write the failing tests:** `test_binary_round_trip` (`bytes(range(256)) * 10`), `test_read_missing_raises_file_not_found`, `test_write_creates_parents`, `test_stat_file_and_dir`, `test_list_dir_entries`, `test_remove_file_and_tree`, `test_exists`, and port E2B's `_path_error` translation tests (is-a-directory, not-a-directory, permission) using the error shapes recorded in sdk-notes.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement with the SDK's native file API where sdk-notes shows it meets the contract; where it doesn't (e.g. symlink handling), use a shell command the way E2B's `_file_entry` does.
- [ ] **Step 4:** Run `uv run pytest tests/test_backend.py && uv run pyright`. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: OpenSandbox filesystem operations"`.

### Task 6: Conformance suite (fake and live)

**Files:**
- Test: `tests/test_conformance.py`

**Interfaces:**
- Consumes: `OpenSandboxBackend`, `kill_sandbox`, `FakeOpenSandbox`.

- [ ] **Step 1:** Port `tests/harness/e2b_sandbox/test_conformance.py`: `TestFakeOpenSandboxBackend(WorkspaceBackendSuite)` over the fake, and `@pytest.mark.opensandbox_live` `TestLiveOpenSandboxBackend(WorkspaceBackendSuite)` with a class-scoped backend killed in teardown.
- [ ] **Step 2:** Run `uv run pytest tests/test_conformance.py`. Expected: fake tier PASS, live tier skipped. Fix the backend (not the suite) for any failures.
- [ ] **Step 3:** Run with the local server: `PYDANTIC_AI_OPENSANDBOX_LIVE=1 OPEN_SANDBOX_DOMAIN=localhost:8080 uv run pytest tests/test_conformance.py`. Expected: live tier PASS. For a rule the real server genuinely can't meet, override the suite's capability fixture with a comment citing the probe, as E2B's live class does; anything else is a backend bug to fix.
- [ ] **Step 4:** Commit: `git commit -m "test: run the workspace conformance suite against OpenSandbox"`.

### Task 7: `OpenSandbox` capability

**Files:**
- Create: `src/pydantic_ai_opensandbox/_capability.py`; modify `__init__.py` to export `OpenSandbox`, `OpenSandboxBackend`
- Test: `tests/test_capability.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(kw_only=True)
  class OpenSandbox(AbstractCapability[AgentDepsT]):
      image: str = DEFAULT_IMAGE
      allow_internet_access: bool = True
      sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT
      working_dir: str | None = None
      env: Mapping[str, str] | None = field(default=None, repr=False)
      def backend(self, ref: WorkspaceRef) -> OpenSandboxBackend
      async def destroy(self, ref: WorkspaceRef) -> None
      def get_workspace(self, ctx: RunContext[AgentDepsT], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None
  ```
  A line-by-line port of `e2b_sandbox/_capability.py`: same `__post_init__` checks and `UserError` messages (with `OpenSandbox` in place of `E2BSandbox`), same docstrings adapted (field docstrings say "OpenSandbox server" where E2B's say "E2B"; `sandbox_timeout` docstring says an expired sandbox is deleted, a paused one is resumed on attach).

- [ ] **Step 1: Write the failing tests**, porting `tests/harness/e2b_sandbox/test_e2b_sandbox.py`: `defer_loading=True` → `UserError`; bad `sandbox_timeout`, relative `working_dir`, non-string `env` → `UserError`; `get_workspace(ref=None)` returns an `OpenSandboxBackend` with the fields passed through and no I/O; `get_workspace(ref=WorkspaceRef(provider='e2b', id='x'))` → `None`; `destroy` with another provider → `ValueError`; `destroy` kills via the fake; `repr` hides `env`; an `Agent(TestModel(), capabilities=[OpenSandbox(), Shell()])` run over the fake executes a shell tool call inside the fake sandbox and `result` exposes the workspace ref.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest && uv run pyright && uv run coverage run -m pytest && uv run coverage report --fail-under=100`. Expected: all PASS, 100%.
- [ ] **Step 5:** Commit: `git commit -m "feat: add OpenSandbox capability"`.

### Task 8: README and end-to-end agent run

**Files:**
- Modify: `README.md`
- Create: `examples/coder_e2e.py`, `tests/test_live_agent.py`

- [ ] **Step 1:** Write `README.md` mirroring the sections of `e2b_sandbox/README.md` (Install, Quick start, Continue in the same sandbox, Clean up, Network access, Prepare a sandbox before the run, Testing), adapted: install from the local path for now, `OPEN_SANDBOX_DOMAIN` / `OPEN_SANDBOX_API_KEY`, how to start `opensandbox-server` locally, the default image and how to pass another, the egress-sidecar requirement for `allow_internet_access=False`.
- [ ] **Step 2:** Write `examples/coder_e2e.py`: `Agent('anthropic:claude-opus-5-5', capabilities=[OpenSandbox(), Coder()])` asked to "Create /tmp/e2e/fib.py that prints the first 10 Fibonacci numbers, run it, and report the output", then print the output and `result` workspace ref, then verify independently by attaching `OpenSandboxBackend(ref=...)` and reading the file, then `destroy`.
- [ ] **Step 3:** Run it with `ANTHROPIC_API_KEY` set and the local server up: `uv run python examples/coder_e2e.py`. Expected: the agent reports `0 1 1 2 3 5 8 13 21 34`, the independent read finds `fib.py`, and `docker ps` showed the sandbox container during the run. If no `ANTHROPIC_API_KEY` is available, stop and ask the user for a model/key.
- [ ] **Step 4:** Add `tests/test_live_agent.py` (marked `opensandbox_live`) doing the same with a real model and asserting the file exists, so the e2e check is repeatable. Verify the deny policy once by hand: `OpenSandbox(allow_internet_access=False)` + `run(['curl', '-sS', 'https://example.com'])` fails; record the result in `docs/sdk-notes.md`.
- [ ] **Step 5:** Run `uv run ruff check . && uv run pyright && uv run pytest`. Expected: clean. Commit: `git commit -m "docs: README and end-to-end agent example"`.
