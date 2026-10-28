# `pydantic-ai-opensandbox` design

## Intent

Let a Pydantic AI agent run its shell commands and file edits in an
[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) sandbox, the same way
`pydantic_ai_harness.e2b_sandbox.E2BSandbox` does for E2B:

```python
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_opensandbox import OpenSandbox

agent = Agent('anthropic:claude-opus-5-5', capabilities=[OpenSandbox(), Coder()])
```

**Why:** OpenSandbox is open source and runs locally on Docker or on your own Kubernetes cluster.
Teams whose infrastructure is on Kubernetes can keep the code their agents run inside that
infrastructure instead of sending it to a hosted service.

**Why a separate package:** Pydantic AI maintainers accept a new first-party sandbox only when the
provider's team helps maintain it (see pydantic/pydantic-ai#10922, where the Boat sandbox went this
route as `pydantic-ai-boat`). This package ships the integration now. A GitHub issue on
pydantic/pydantic-ai announces it, the package gets listed under "Third-party workspaces" in
`docs/workspace.md`, and if the OpenSandbox team agrees to co-maintain it, the code can be moved into
`pydantic_ai_harness` with little change. **Because of that, the code must follow the harness's
conventions closely** (style, naming, docstring voice, error translation, test layout), as if it
already lived at `src/pydantic_ai_harness/pydantic_ai_harness/opensandbox_sandbox/`.

## Goals

- `OpenSandbox` is a capability whose constructor matches `E2BSandbox`'s as closely as OpenSandbox
  allows (the user's explicit requirement). Same field names, types, defaults, validation, and
  docstring style; differences are only where OpenSandbox has no equivalent concept.
- `OpenSandboxBackend` implements `WorkspaceBackend`, `SupportsCommands`, `SupportsFilesystem`, and
  passes core's conformance suite, `pydantic_ai.workspaces.conformance.WorkspaceBackendSuite`.
- Works with `Coder`, `Shell`, `FileSystem`, and every other harness capability that uses
  `ctx.workspace`.
- Continuing in the same sandbox works like E2B: `WorkspaceRef(provider='opensandbox', id=<sandbox id>)`,
  reattach on later runs, and `OpenSandbox().destroy(ref)` to kill one.
- 100% test coverage, type-checked with pyright strict, linted with ruff, matching pydantic-ai's
  configuration.

## Non-goals (v1)

- Egress allow-lists beyond on/off, pause/resume, snapshots, pools, volumes, Credential Vault, fast-sandbox
  templates, resource limits. These can come later as new fields.
- Durable execution (Temporal, etc.) beyond what the base workspace machinery gives for free.
- Changing anything in pydantic-ai itself, except a later docs PR adding the catalog entry.

## Anti-goals

- An interface that looks different from `E2BSandbox` for no reason.
- Code that couldn't be moved into the harness without a rewrite (different error types, different
  patterns, mocked-only tests).

## Constructor: mapping from `E2BSandbox`

| `E2BSandbox` | `OpenSandbox` | Notes |
|---|---|---|
| `template: str \| None = None` | `image: str = DEFAULT_IMAGE` | OpenSandbox creates sandboxes from container images and requires one. The default should have Python, git, and `sh`, ideally ripgrep; the builder picks it after checking what OpenSandbox publishes (for example its code-interpreter image). An image that can't be pulled raises `WorkspaceUnavailableError` on first use, as E2B does for an unknown template. |
| `allow_internet_access: bool = True` | `allow_internet_access: bool = True` | `False` maps to `NetworkPolicy(default_action='deny')`. The builder verifies this actually blocks egress on the local Docker server; if the server can't enforce it, raise `WorkspaceUnavailableError` rather than silently allowing traffic. |
| `sandbox_timeout: int = 3600` | `sandbox_timeout: int = 3600` | Seconds. Passed as `timeout=timedelta(...)` on create; on attach, call `renew` so the lifetime is applied on attach too, like E2B. |
| `working_dir: str \| None = None` | `working_dir: str \| None = None` | Same semantics: absolute, created on a new sandbox, `None` uses the sandbox's own. |
| `env: Mapping[str, str] \| None` | `env: Mapping[str, str] \| None` | Same semantics, `repr=False`, nothing read from the host. |
| (E2B reads `E2B_API_KEY` from the environment) | (SDK reads `OPEN_SANDBOX_DOMAIN` and `OPEN_SANDBOX_API_KEY`) | No connection fields in v1, matching E2B, whose capability has none. |

Same `__post_init__` checks as E2B (`defer_loading` refused, `check_integer`, `check_working_dir`,
`check_env_types`). Because `pydantic_ai_harness._workspace_provider` is private, the package keeps
its own copies of the helpers it needs rather than importing private harness modules.

Methods match E2B: `get_workspace(ctx, *, ref)` (no I/O; returns `None` for a ref from another
provider), `backend(ref)`, `destroy(ref)` (raises `ValueError` for another provider's ref).

## Backend

Mirror `e2b_sandbox/_backend.py` structure and behavior, translating OpenSandbox SDK
(`opensandbox` on PyPI, async API) calls:

- Lazy create/attach on first operation, guarded so concurrent first calls create one sandbox.
- `ref` is `None` until the sandbox exists.
- SDK errors translated to `WorkspaceUnavailableError` (sandbox gone, auth failure, server
  unreachable, image refused), `WorkspaceTimeoutError` (command timeout, with output so far),
  `FileNotFoundError` / other `OSError`s for file operations.
- `run(...)` honors the `SupportsCommands` contract (cwd, env, timeout, output limits, cancellation
  kills the process) the way E2B's does. Where the OpenSandbox SDK can't do something natively, fall
  back the way E2B does (e.g. `setsid` probing) rather than weakening the contract.
- File operations use the SDK's native file API where it satisfies the contract; otherwise rely on
  the shell-derived fallback.

## Package layout

New repo at `/Users/ilayu/Documents/open_source/pydantic-ai-opensandbox`, shaped like
[`pydantic-ai-boat`](https://github.com/zozo123/pydantic-ai-boat):

```
pyproject.toml          # deps: pydantic-ai-slim (workspace support), opensandbox; dev: pytest, anyio, pyright, ruff, coverage
README.md               # mirrors e2b_sandbox/README.md sections, adapted
src/pydantic_ai_opensandbox/
    __init__.py         # exports OpenSandbox, OpenSandboxBackend
    _capability.py
    _backend.py
    py.typed
tests/
    test_conformance.py # WorkspaceBackendSuite against a live local server
    test_capability.py  # validation, ref routing, destroy
    test_backend.py     # error translation and edge paths
```

Python and dependency version floors: match pydantic-ai's supported range (3.11+) and whatever
pydantic-ai version first exposes the public `pydantic_ai.workspaces` API used here.

## Testing

(Decided by harness precedent, `tests/harness/e2b_sandbox/fake_e2b.py`.)

- **Offline tier, runs in CI, carries the 100% coverage:** `WorkspaceBackendSuite` and the unit tests
  run against an in-process fake of the OpenSandbox server/SDK surface the backend uses, built the way
  the harness's `fake_e2b.py` is. Error paths raise the real `opensandbox` exception types.
- **Live tier, opt-in:** the same `WorkspaceBackendSuite` runs against a real local server
  (`uvx opensandbox-server` with Docker), skipped unless `OPEN_SANDBOX_DOMAIN` is set and reachable,
  like the harness's opt-in sandbox live tests. It proves the fake matches reality; it isn't required
  for coverage.

## Decisions marked as Claude's call

- **Egress deny:** `allow_internet_access=False` sends `NetworkPolicy(default_action='deny')` and trusts
  the server, as `E2BSandbox` trusts E2B. No per-create probe. The builder checks once by hand on the
  local Docker server that egress is really blocked; the README states it depends on the server's
  egress sidecar being enabled.
- **Attach to a paused sandbox:** if the sandbox's state is paused, attach calls `resume` (the SDK has
  it), keeping `E2BSandbox`'s "attaching resumes it" promise. An expired/deleted sandbox raises
  `WorkspaceUnavailableError`. The package itself never pauses sandboxes.

## Left to the builder

- The default image (see the table).
- Exact SDK calls for commands and files, after reading the SDK and comparing with E2B's backend.
- How to start the local server for tests (config file, Docker runtime), documented in the README.
- Whether some error paths need a small fake server instead of real-SDK-exception tests, as long as
  coverage stays at 100%.

## Also in scope

- A short issue on pydantic/pydantic-ai announcing the package. Filed as a normal
  external-contributor issue, no labels. Approved draft:

  > **Title:** Add an OpenSandbox sandbox capability
  >
  > [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) is an open source sandbox platform
  > (an AAIF project) that runs locally on Docker or on your own Kubernetes cluster. That makes it a
  > good fit for teams whose infra is on Kubernetes and who can't send agent code to a hosted service.
  > I'd like `Agent(..., capabilities=[OpenSandbox(), Coder()])` to work the way `E2BSandbox` does
  > today.
  >
  > Per the guidance on #10922, I'll start with a third-party package that passes
  > `WorkspaceBackendSuite`, list it in the third-party workspaces catalog, and reach out to the
  > OpenSandbox maintainers about co-maintaining a first-party integration.
## Done means

The package works end to end locally: the offline and live test tiers pass, and a real agent
(`Agent('anthropic:claude-opus-5-5', capabilities=[OpenSandbox(), Coder()])`) completes a coding task
whose commands and file edits happen inside an OpenSandbox sandbox on the local server.

## Deferred until the issue gets responses

Publishing (GitHub repo owner, PyPI, license, release automation) and the `docs/workspace.md`
"Third-party workspaces" catalog PR. The approach may change depending on what maintainers or the
OpenSandbox team say on the issue.
