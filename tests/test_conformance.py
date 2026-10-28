"""Pydantic AI's workspace backend conformance suite, run against `OpenSandboxBackend`.

`TestFakeOpenSandboxBackend` runs everywhere, over the host-backed fake SDK. `TestLiveOpenSandboxBackend` runs the
same rules against a real OpenSandbox server and is gated by `PYDANTIC_AI_OPENSANDBOX_LIVE=1` (see `conftest.py`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_opensandbox import OpenSandboxBackend

from .fake_opensandbox import FakeOpenSandbox


def _attach(ref: WorkspaceRef) -> WorkspaceBackend:
    return OpenSandboxBackend(ref=ref)


async def _kill(backend: WorkspaceBackend) -> None:
    assert isinstance(backend, OpenSandboxBackend)
    sandbox = await backend.get_sandbox()
    await sandbox.kill()


class TestFakeOpenSandboxBackend(WorkspaceBackendSuite):
    @pytest.fixture
    def backend(self, fake_opensandbox: FakeOpenSandbox) -> OpenSandboxBackend:
        return OpenSandboxBackend()

    @pytest.fixture
    def fresh_backend(self, fake_opensandbox: FakeOpenSandbox) -> Callable[[], WorkspaceBackend]:
        return OpenSandboxBackend

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return _attach

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        return _kill

    @pytest.fixture
    def filesystem_honors_shell_permissions(self) -> bool:
        # The fake runs as the developer, who owns every file; the live server's execd runs as root.
        return False


# The live tier runs without coverage; in CI only its gate runs, to skip it.
@pytest.mark.opensandbox_live
class TestLiveOpenSandboxBackend(WorkspaceBackendSuite):  # pragma: lax no cover
    # Class-scoped so the rules share one sandbox instead of starting one each. The teardown kills it on a loop of
    # its own, because a class-scoped fixture outlives each test's event loop.
    @pytest.fixture(scope='class')
    @classmethod
    def backend(cls) -> Iterator[OpenSandboxBackend]:
        backend = OpenSandboxBackend(sandbox_timeout=900, working_dir='/root/work')
        yield backend
        if backend.ref is not None:
            from pydantic_ai_opensandbox._backend import kill_sandbox

            asyncio.run(kill_sandbox(backend.ref.id))

    @pytest.fixture
    def fresh_backend(self) -> Callable[[], WorkspaceBackend]:
        # Short-lived: the concurrent-first-use rule leaves its sandbox for the server to delete.
        return lambda: OpenSandboxBackend(sandbox_timeout=120, working_dir='/root/work')

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return _attach

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        return _kill


def test_fake_tier_is_rooted_in_tmp_path(fake_opensandbox: FakeOpenSandbox, tmp_path: Path) -> None:
    assert fake_opensandbox.host_root == tmp_path.resolve()
