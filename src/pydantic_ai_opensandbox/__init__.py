"""Run Pydantic AI agent workspaces in [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) sandboxes."""

from ._backend import DEFAULT_IMAGE, DEFAULT_SANDBOX_TIMEOUT, OpenSandboxBackend

__all__ = ('DEFAULT_IMAGE', 'DEFAULT_SANDBOX_TIMEOUT', 'OpenSandboxBackend')
