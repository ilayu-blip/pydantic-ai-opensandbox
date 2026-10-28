"""Shared fixtures for OpenSandbox tests."""

from __future__ import annotations

import os
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from .fake_opensandbox import FakeOpenSandbox


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Gate the live tier, before any live fixture can start a sandbox.

    It runs only with `PYDANTIC_AI_OPENSANDBOX_LIVE=1` and a non-empty `OPEN_SANDBOX_DOMAIN`. Otherwise it skips,
    unless `OPENSANDBOX_REQUIRE_LIVE=1`, where a missing setting fails instead: a live job that tested nothing must
    not pass as a silent skip.
    """
    if item.get_closest_marker('opensandbox_live') is None:
        return
    if os.getenv('PYDANTIC_AI_OPENSANDBOX_LIVE') == '1' and os.getenv('OPEN_SANDBOX_DOMAIN'):  # pragma: no cover - live
        return
    reason = 'requires PYDANTIC_AI_OPENSANDBOX_LIVE=1 and a non-empty OPEN_SANDBOX_DOMAIN'
    if os.getenv('OPENSANDBOX_REQUIRE_LIVE') == '1':  # pragma: no cover - live job
        pytest.fail(f'OPENSANDBOX_REQUIRE_LIVE=1 but the live tier cannot run: it {reason}.')
    pytest.skip(reason)


class _PoisonedOpenSandbox(types.ModuleType):
    """An `opensandbox` stand-in that fails loudly on any attribute access.

    The real SDK is installed for the live tier, so a unit test that forgot the `fake_opensandbox` fixture would
    otherwise reach a real server configured in the developer's environment.
    """

    def __getattr__(self, name: str) -> object:  # pragma: no cover - tripwire, hit only by a misbehaving test
        raise AssertionError(
            'An OpenSandbox unit test touched the real `opensandbox` package. '
            'Use the `fake_opensandbox` fixture, or mark the test `opensandbox_live`.'
        )


@pytest.fixture(autouse=True)
def _no_real_opensandbox(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Poison `opensandbox` for every test except the opt-in live tier."""
    if 'opensandbox_live' in request.keywords:  # pragma: no cover - live tier
        return
    monkeypatch.setattr('pydantic_ai_opensandbox._backend.opensandbox', _PoisonedOpenSandbox('opensandbox'))


@pytest.fixture
def fake_opensandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    """Inject a host-backed fake `opensandbox` module rooted at `tmp_path`, and yield its control surface."""
    # Resolved so `pwd -P` matches it on hosts whose temp directory sits behind a symlink.
    control = FakeOpenSandbox(host_root=tmp_path.resolve())
    monkeypatch.setattr('pydantic_ai_opensandbox._backend.opensandbox', control.module)
    try:
        yield control
    finally:
        control.close()


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'
