"""Shared fixtures for OpenSandbox tests."""

from __future__ import annotations

import os

import pytest


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
