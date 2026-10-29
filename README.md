# pydantic-ai-opensandbox

Run your [Pydantic AI](https://pydantic.dev/docs/ai/) agent's commands and file edits in an isolated
[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) sandbox, on your own Docker host or Kubernetes
cluster, instead of on your machine.

`OpenSandbox` is a [Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/) workspace capability, with the
same interface as the harness's
[`E2BSandbox`](https://github.com/pydantic/pydantic-ai/tree/main/src/pydantic_ai_harness/pydantic_ai_harness/e2b_sandbox/).
`Coder`, `Shell`, `FileSystem` and every other capability that uses `ctx.workspace` run inside the sandbox.

> This is a community package, not part of Pydantic AI. See
> [pydantic/pydantic-ai#11769](https://github.com/pydantic/pydantic-ai/issues/11769).

## Install

The package is not on PyPI yet. Install it from a checkout:

```bash
uv add ./pydantic-ai-opensandbox "pydantic-ai-harness" "pydantic-ai-slim[anthropic]"
```

The `anthropic` extra is there because the examples use an Anthropic model; swap it for your model provider's
extra.

## Run an OpenSandbox server

Locally, on Docker:

```bash
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
# On macOS (Docker Desktop), set `[proxy] resolve_internal = false` in ~/.sandbox.toml.
uvx opensandbox-server --config ~/.sandbox.toml
```

On Kubernetes, follow OpenSandbox's
[Kubernetes deployment guide](https://github.com/opensandbox-group/OpenSandbox/tree/main/kubernetes).

Then point the SDK at the server. Both variables are read by the OpenSandbox SDK:

```bash
export OPEN_SANDBOX_DOMAIN=localhost:8080   # the server's address
export OPEN_SANDBOX_API_KEY=...             # if the server sets `api_key`
```

## Quick start

```python
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_opensandbox import OpenSandbox

agent = Agent('anthropic:claude-opus-5-5', capabilities=[OpenSandbox(), Coder()])
result = agent.run_sync('Clone https://github.com/pydantic/pydantic-ai and summarize how capabilities work.')
```

`Coder`'s shell and file tools now run in the sandbox, not on your machine. With `Coder`, `RepoContext` creates the
sandbox when the run starts, even without a tool call; use `Coder(repo_context=False)` for lazy creation. The
sandbox keeps running after the run ends; see [Clean up](#clean-up).

A sandbox lives for 1 hour (`sandbox_timeout=3600`), then the server deletes it. The server's
`max_sandbox_timeout_seconds` caps the lifetime.

The default image is `python:3.12` (Debian, with bash, git, Python and curl, but not ripgrep). Commands run as
root and start in `/`, the image's default directory. Pass a project directory, such as
`OpenSandbox(working_dir='/root/project')`; it is created for you on a new sandbox. Pass any image the server can
pull as `image=`; it needs `/bin/sh`, `/bin/bash` and a writable `/tmp`. Pull large images on the server ahead of
time: the first run that uses an image waits for the pull.

## Continue in the same sandbox

```python
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_opensandbox import OpenSandbox

agent = Agent('anthropic:claude-opus-5-5', capabilities=[OpenSandbox(), Coder()])
result = agent.run_sync('Clone https://github.com/pydantic/pydantic-ai and summarize how capabilities work.')

followup = agent.run_sync(
    'Which capability would you add next, and where would it live?',
    message_history=result.all_messages(),
)
```

The follow-up run finds the sandbox in the message history and works in it, so the clone is still there. Attaching
renews the sandbox's lifetime, and resumes it if it was paused. Without the history, a run starts a new sandbox. If
the sandbox has been deleted, the run raises `WorkspaceUnavailableError` instead of starting over in an empty one.

To come back without the message history, store `result.workspace.ref` and pass it back as `workspace=`. Pass
`workspace='new'` to start a fresh sandbox even when the message history names one.

`image` and `allow_internet_access` only shape a new sandbox. `sandbox_timeout` also applies when you reattach,
and `working_dir` and `env` apply to every command.

## Prepare a sandbox before the run

```python
import asyncio

from pydantic_ai import Agent
from pydantic_ai.workspaces import Workspace
from pydantic_ai_harness.coder import Coder
from pydantic_ai_opensandbox import OpenSandbox, OpenSandboxBackend

agent = Agent('anthropic:claude-opus-5-5', capabilities=[OpenSandbox(), Coder()])


async def main() -> None:
    backend = OpenSandboxBackend(working_dir='/root/project')
    try:
        workspace = Workspace(backend)
        await workspace.write_text('calc.py', 'def add(a, b):\n    return a - b\n')
        install = await workspace.run(['pip', 'install', 'pytest'], timeout=300)
        if install.exit_code != 0:
            raise RuntimeError(f'pip install failed: {install.stderr}')
        result = await agent.run('Fix the bug in calc.py.', workspace=backend)
        print(result.output)
    finally:
        if backend.ref is not None:
            await OpenSandbox().destroy(backend.ref)


if __name__ == '__main__':
    asyncio.run(main())
```

Already have an `opensandbox.Sandbox`? Pass `workspace=OpenSandboxBackend(sandbox=sandbox)` to a run.
`OpenSandbox`'s settings don't apply to it; pass `working_dir=` and `env=` to the backend.

## How commands run

- Each command runs through execd's `bash -c`; an argv list is shell-quoted first. Output is redirected to files
  under `/tmp` in the sandbox and read back byte-exact, because execd's own stream splits output into lines.
- On a timeout or cancellation, the backend interrupts the command's process group through execd. A command
  that has not reported its execution id yet is fenced or killed through a claim directory, so it cannot start
  late. A fenced command leaves its empty claim directory (`/tmp/.pydantic-ai-opensandbox-*`) in the sandbox, on
  purpose: removing it would let a late start through. The sandbox keeps running.
- `WorkspaceTimeoutError` carries the command's output so far, up to the 10 MiB cap.
- A command whose combined output passes 10 MiB is stopped the same way and raises `WorkspaceOutputLimitError`,
  with the first 64 KiB of each stream. Redirect large output to a file instead.
- A command killed by a signal returns `exit_code=-1`, as execd reports it.
- A sandbox this package creates gets `EXECD_API_GRACE_SHUTDOWN=0s`, which removes the 1-second delay execd adds
  after every command. Pass your own value in `env` to override it.
- The OpenSandbox SDK logs every failed request at ERROR on the `opensandbox` logger, including answers the
  backend expects and handles, such as the "file not found" it checks before writing a new file. Raise that
  logger's level to quiet them: `logging.getLogger('opensandbox').setLevel(logging.CRITICAL)`.
- Files: the file API runs as root, so Unix permissions do not restrict file tools. Writing a file keeps its
  mode; a new file gets `644`. Reading a FIFO raises `OSError` instead of waiting for a writer.

## Network access

`OpenSandbox(allow_internet_access=False)` creates the sandbox with a deny-all egress policy. The server enforces
it with its egress sidecar, which needs `[docker] network_mode = "bridge"` (the default) on Docker.

## Clean up

The sandbox keeps running after the run ends; Pydantic AI does not delete it. Its `sandbox_timeout` bounds how
long. Delete it sooner with the ref you stored:

```python
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai_opensandbox import OpenSandbox


async def delete_sandbox(ref: WorkspaceRef) -> None:
    await OpenSandbox().destroy(ref)
```

`destroy(ref)` deletes by ID without attaching, a paused sandbox too, and returns quietly if it is already gone.
Creating a sandbox logs `Created OpenSandbox sandbox <id>` at INFO on the `pydantic_ai_opensandbox._backend`
logger, so the id is on record even if the run ends before it is stored.

If the server never answers a create (`TimeoutError` after 300 seconds, for example while it pulls a large image),
it may still finish creating a sandbox that no ref or log entry names. That sandbox is deleted when its
`sandbox_timeout` runs out; list and delete it sooner with the OpenSandbox CLI or API.

## Configuration

| Option | What it does |
| --- | --- |
| `image` | Container image for a new sandbox. Default: `python:3.12`. An image the server cannot pull fails on first use with `WorkspaceUnavailableError`. |
| `allow_internet_access` | Whether a new sandbox can reach the internet. Default: `True`. |
| `sandbox_timeout` | Seconds the sandbox lives before the server deletes it, set on create and renewed on reattach. Default: `3_600`. |
| `working_dir` | Absolute directory commands start in and relative paths resolve against. Default: `None`, the image's own. Created on a new sandbox; on an attached sandbox it must already exist. |
| `env` | Environment variables every command gets. Default: `None`. Nothing from your machine's environment reaches the sandbox. The agent's commands can read these values, so leave out secrets the agent should not see. |

`OpenSandbox` needs asyncio: the OpenSandbox SDK runs every request on asyncio, so under Trio every operation,
including `destroy`, raises `UserError` before it reaches the server.

## Development

```bash
uv sync
uv run pytest                      # offline tier: a host-backed fake of the SDK, 100% coverage
uv run ruff check . && uv run pyright src tests
```

The live tier runs the same workspace conformance suite against a real server:

```bash
PYDANTIC_AI_OPENSANDBOX_LIVE=1 OPEN_SANDBOX_DOMAIN=localhost:8080 OPEN_SANDBOX_API_KEY=... uv run pytest -m opensandbox_live
```

`examples/coder_e2e.py` runs a real agent with `Coder` in a sandbox. `docs/sdk-notes.md` records the server and SDK
behavior the backend relies on, and `scripts/probe_sdk.py` re-checks it.
