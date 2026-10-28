"""Capability that supplies an OpenSandbox sandbox to an agent run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef

from ._backend import DEFAULT_IMAGE, DEFAULT_SANDBOX_TIMEOUT, PROVIDER, OpenSandboxBackend, kill_sandbox
from ._workspace_provider import check_env_types, check_integer, check_working_dir


@dataclass(kw_only=True)
class OpenSandbox(AbstractCapability[AgentDepsT]):
    """Supply an isolated [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) sandbox as the run's workspace.

    The OpenSandbox server's address and API key come from the `OPEN_SANDBOX_DOMAIN` and `OPEN_SANDBOX_API_KEY`
    environment variables. A run with no reference creates a fresh sandbox. Pass a `WorkspaceRef` supplied by the
    application to attach to an environment managed elsewhere.

    This capability supplies execution only. Compose it with tools or capabilities that use the workspace, such as
    `Coder`, `Shell`, or `FileSystem`. Shell commands run under `sh -c` inside the sandbox's `bash -c`.
    """

    image: str = DEFAULT_IMAGE
    """Container image a newly created sandbox runs; `python:3.12` by default.

    An image the server cannot pull raises `WorkspaceUnavailableError` on first use.
    """

    allow_internet_access: bool = True
    """Whether a newly created workspace may reach the internet."""

    sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT
    """Total lifetime of the sandbox in seconds, applied on create and renewed on attach.

    When it runs out, the server deletes the sandbox. The default is 1 hour; the server's
    `max_sandbox_timeout_seconds` caps it.
    """

    working_dir: str | None = None
    """Absolute directory commands start in and relative paths resolve against; `None` uses the sandbox's own."""

    env: Mapping[str, str] | None = field(default=None, repr=False)
    """Environment variables every command gets, also on an attached workspace; nothing is read from the host.

    The agent's commands can read them, and a new sandbox keeps them for every process it runs.
    """

    def __post_init__(self) -> None:
        if self.defer_loading:
            raise UserError(
                '`OpenSandbox` does not support `defer_loading=True`: '
                'the workspace is selected before deferred capabilities load.'
            )
        check_integer('sandbox_timeout', self.sandbox_timeout)
        check_working_dir(self.working_dir)
        try:
            check_env_types(self.env)
        except TypeError:
            raise UserError('`env` keys and values must be strings.') from None

    def backend(self, ref: WorkspaceRef) -> OpenSandboxBackend:
        """Attach lazily to an existing OpenSandbox sandbox."""
        return OpenSandboxBackend(
            ref=ref, sandbox_timeout=self.sandbox_timeout, working_dir=self.working_dir, env=self.env
        )

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Kill a sandbox by ID, including a paused one, without attaching."""
        if ref.provider != PROVIDER:
            raise ValueError(f'unsupported workspace provider {ref.provider!r}; expected {PROVIDER!r}')
        await kill_sandbox(ref.id)

    def get_workspace(self, ctx: RunContext[AgentDepsT], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None:
        """Build the backend for this run. No I/O here: it attaches or creates on first use."""
        del ctx
        if ref is not None and ref.provider != PROVIDER:
            return None
        return OpenSandboxBackend(
            ref=ref,
            image=self.image,
            allow_internet_access=self.allow_internet_access,
            sandbox_timeout=self.sandbox_timeout,
            working_dir=self.working_dir,
            env=self.env,
        )
