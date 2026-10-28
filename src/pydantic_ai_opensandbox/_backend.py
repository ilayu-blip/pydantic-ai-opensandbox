"""An OpenSandbox sandbox behind Pydantic AI's `WorkspaceBackend` protocol."""

from __future__ import annotations

try:
    import opensandbox
except ImportError as _import_error:  # pragma: no cover - the SDK is a required dependency
    raise ImportError('Install `pydantic-ai-opensandbox` with its `opensandbox` dependency.') from _import_error

__all__ = ('opensandbox',)
