"""The fake SDK behaves like the server recorded in `docs/sdk-notes.md`, so backend tests over it mean something."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import timedelta
from pathlib import Path

import pytest
from opensandbox.exceptions import SandboxApiException, SandboxConnectionException, SandboxReadyTimeoutException
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import DirectoryListEntry, WriteEntry

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


async def test_fake_server_timeout_kills_the_command(fake_opensandbox: FakeOpenSandbox) -> None:
    sandbox = await _sandbox(fake_opensandbox)
    execution = await sandbox.commands.run(
        'echo started; sleep 30', opts=RunCommandOpts(timeout=timedelta(seconds=0.2))
    )
    assert (execution.exit_code, [m.text for m in execution.logs.stdout]) == (-1, ['started'])


async def test_fake_file_errors_match_the_server(fake_opensandbox: FakeOpenSandbox, tmp_path: Path) -> None:
    """The server's answers recorded in `docs/sdk-notes.md`, which the backend stats first to avoid."""
    sandbox = await _sandbox(fake_opensandbox)
    root = tmp_path.resolve()
    (root / 'dir').mkdir()
    (root / 'file').write_bytes(b'x')
    (root / 'link').symlink_to('dir')
    files = sandbox.files

    async def status(call: Awaitable[object]) -> tuple[int | None, str]:
        with pytest.raises(SandboxApiException) as raised:
            await call
        return raised.value.status_code, raised.value.error.code

    assert await status(files.read_bytes(str(root / 'missing'))) == (404, 'FILE_NOT_FOUND')
    with pytest.raises(SandboxConnectionException):
        await files.read_bytes(str(root / 'dir'))
    assert await status(files.write_file(str(root / 'file' / 'child'), b'x')) == (500, 'RUNTIME_ERROR')
    assert await status(files.write_file(str(root / 'dir'), b'x')) == (500, 'RUNTIME_ERROR')
    assert await status(files.list_directory(DirectoryListEntry(path=str(root / 'link'), depth=1))) == (
        400,
        'INVALID_REQUEST_BODY',
    )
    assert await status(files.list_directory(DirectoryListEntry(path=str(root / 'file'), depth=1))) == (
        400,
        'INVALID_REQUEST_BODY',
    )
    assert await status(files.create_directories([WriteEntry(path=str(root / 'file'))])) == (500, 'RUNTIME_ERROR')
    assert await status(files.delete_files([str(root / 'link')])) == (500, 'RUNTIME_ERROR')
    await files.delete_directories([str(root / 'file'), str(root / 'missing')])
    assert not (root / 'file').exists()


async def test_fake_connect_cannot_reach_a_paused_sandbox(fake_opensandbox: FakeOpenSandbox) -> None:
    sandbox = await _sandbox(fake_opensandbox)
    fake_opensandbox.pause(sandbox.id)
    with pytest.raises(SandboxReadyTimeoutException):
        await fake_opensandbox.module.Sandbox.connect(sandbox.id, connect_timeout=timedelta(seconds=1))
