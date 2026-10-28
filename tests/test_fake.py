"""The fake SDK behaves like the server recorded in `docs/sdk-notes.md`, so backend tests over it mean something."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.execd import RunCommandOpts

from .fake_opensandbox import FakeOpenSandbox, FakeSandbox


async def _sandbox(fake: FakeOpenSandbox) -> FakeSandbox:
    sandbox = await fake.module.Sandbox.create('python:3.12', timeout=timedelta(seconds=60), ready_timeout=timedelta(1))
    assert isinstance(sandbox, FakeSandbox)
    return sandbox


async def test_fake_runs_command_and_files(fake_opensandbox: FakeOpenSandbox, tmp_path: Path) -> None:
    sandbox = await _sandbox(fake_opensandbox)
    execution = await sandbox.commands.run("printf 'a\\nb'; exit 3")
    # execd splits output into lines and drops the terminators.
    assert (execution.exit_code, [m.text for m in execution.logs.stdout]) == (3, ['a', 'b'])
    path = str(tmp_path.resolve() / 'nested' / 'file.bin')
    await sandbox.files.write_file(path, b'\x00\xff')
    assert await sandbox.files.read_bytes(path) == b'\x00\xff'
    assert (await sandbox.files.get_file_info([path]))[path].mode == 755


async def test_fake_refuses_argv_and_missing_working_directory(fake_opensandbox: FakeOpenSandbox) -> None:
    sandbox = await _sandbox(fake_opensandbox)
    with pytest.raises(SandboxApiException) as refused:
        await sandbox.commands.run(['true'])
    assert refused.value.status_code == 400
    with pytest.raises(SandboxApiException, match='working directory does not exist'):
        await sandbox.commands.run('true', opts=RunCommandOpts(working_directory='/nonexistent-dir-for-fake'))
