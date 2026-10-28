"""`OpenSandboxBackend` behavior the conformance suite does not pin, over the fake SDK.

These are unit tests rather than live ones because each forces a server answer (a refused key, a lost sandbox,
an unreachable domain) that a healthy local server never gives on demand.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from opensandbox.exceptions import SandboxConnectionException
from opensandbox.models.sandboxes import NetworkPolicy
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    FileEntry,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from pydantic_ai_opensandbox import OpenSandboxBackend

from .fake_opensandbox import FakeCommands, FakeOpenSandbox, FakeSandbox, api_error


async def _existing(fake: FakeOpenSandbox) -> FakeSandbox:
    """A sandbox created outside the backend under test, as another run or the application would."""
    sandbox = await fake.module.Sandbox.create('python:3.12', timeout=None, ready_timeout=timedelta(seconds=1))
    assert isinstance(sandbox, FakeSandbox)
    return sandbox


def _ref(sandbox_id: str) -> WorkspaceRef:
    return WorkspaceRef(provider='opensandbox', id=sandbox_id)


class TestAcquisition:
    async def test_construction_does_no_io(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        assert backend.ref is None
        assert fake_opensandbox.create_calls == []

    async def test_first_use_creates_sandbox(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend(image='python:3.13', env={'A': '1'}, sandbox_timeout=120)
        await backend.working_dir()
        [call] = fake_opensandbox.create_calls
        assert call.image == 'python:3.13'
        assert call.timeout == timedelta(seconds=120)
        # execd's 1 s grace period before closing each command stream is switched off; user env wins.
        assert call.env == {'EXECD_API_GRACE_SHUTDOWN': '0s', 'A': '1'}
        assert call.network_policy is None
        assert backend.ref == _ref('sbx-1')

    async def test_default_image_and_lifetime(self, fake_opensandbox: FakeOpenSandbox) -> None:
        await OpenSandboxBackend().working_dir()
        [call] = fake_opensandbox.create_calls
        assert (call.image, call.timeout) == ('python:3.12', timedelta(seconds=3600))

    async def test_user_env_can_override_the_grace_period(self, fake_opensandbox: FakeOpenSandbox) -> None:
        await OpenSandboxBackend(env={'EXECD_API_GRACE_SHUTDOWN': '200ms'}).working_dir()
        assert fake_opensandbox.create_calls[0].env == {'EXECD_API_GRACE_SHUTDOWN': '200ms'}

    async def test_no_internet_sends_deny_policy(self, fake_opensandbox: FakeOpenSandbox) -> None:
        await OpenSandboxBackend(allow_internet_access=False).working_dir()
        assert fake_opensandbox.create_calls[0].network_policy == NetworkPolicy(defaultAction='deny')

    async def test_concurrent_first_use_creates_one_sandbox(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        async with anyio.create_task_group() as group:
            for _ in range(5):
                group.start_soon(backend.working_dir)
        assert len(fake_opensandbox.create_calls) == 1

    async def test_working_dir_is_created_on_a_new_sandbox(
        self, fake_opensandbox: FakeOpenSandbox, tmp_path: Path
    ) -> None:
        project = str(tmp_path.resolve() / 'project')
        backend = OpenSandboxBackend(working_dir=project)
        assert await backend.working_dir() == project
        assert Path(project).is_dir()

    async def test_working_dir_defaults_to_the_sandbox_cwd(
        self, fake_opensandbox: FakeOpenSandbox, tmp_path: Path
    ) -> None:
        assert await OpenSandboxBackend().working_dir() == str(tmp_path.resolve())

    async def test_attach_renews_lifetime_without_creating(self, fake_opensandbox: FakeOpenSandbox) -> None:
        existing = await _existing(fake_opensandbox)
        backend = OpenSandboxBackend(ref=_ref(existing.id), sandbox_timeout=900)
        await backend.working_dir()
        assert fake_opensandbox.connect_calls == [existing.id]
        assert existing.renewals == [timedelta(seconds=900)]
        assert len(fake_opensandbox.create_calls) == 1  # only `_existing`'s own
        assert backend.ref == _ref(existing.id)

    async def test_attach_resumes_paused_sandbox(self, fake_opensandbox: FakeOpenSandbox) -> None:
        existing = await _existing(fake_opensandbox)
        fake_opensandbox.pause(existing.id)
        await OpenSandboxBackend(ref=_ref(existing.id)).working_dir()
        assert fake_opensandbox.resume_calls == [existing.id]
        assert existing.state == 'Running'

    async def test_attach_to_deleted_sandbox_is_unavailable(self, fake_opensandbox: FakeOpenSandbox) -> None:
        existing = await _existing(fake_opensandbox)
        fake_opensandbox.kill(existing.id)
        with pytest.raises(WorkspaceUnavailableError, match="OpenSandbox sandbox 'sbx-1' no longer exists"):
            await OpenSandboxBackend(ref=_ref(existing.id)).working_dir()
        assert fake_opensandbox.connect_calls == []
        assert len(fake_opensandbox.create_calls) == 1

    async def test_attach_to_unknown_sandbox_is_unavailable(self, fake_opensandbox: FakeOpenSandbox) -> None:
        with pytest.raises(WorkspaceUnavailableError, match='no longer exists'):
            await OpenSandboxBackend(ref=_ref('never-existed')).working_dir()

    async def test_attached_sandbox_is_not_given_the_create_settings(self, fake_opensandbox: FakeOpenSandbox) -> None:
        existing = await _existing(fake_opensandbox)
        await OpenSandboxBackend(ref=_ref(existing.id), image='other', allow_internet_access=False).working_dir()
        assert len(fake_opensandbox.create_calls) == 1

    async def test_get_sandbox_returns_the_sdk_handle(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        sandbox = await backend.get_sandbox()
        assert sandbox is fake_opensandbox.sandboxes['sbx-1']
        assert await backend.get_sandbox() is sandbox

    async def test_a_passed_sandbox_is_used_as_is(self, fake_opensandbox: FakeOpenSandbox) -> None:
        existing = await _existing(fake_opensandbox)
        backend = OpenSandboxBackend(sandbox=existing)  # pyright: ignore[reportArgumentType]
        assert backend.ref == _ref(existing.id)
        assert await backend.get_sandbox() is existing
        assert fake_opensandbox.connect_calls == []


class TestAcquisitionErrors:
    async def test_unreachable_server_is_unavailable(self, fake_opensandbox: FakeOpenSandbox) -> None:
        fake_opensandbox.fail('create', SandboxConnectionException('Network connectivity error: refused'))
        with pytest.raises(WorkspaceUnavailableError, match='OPEN_SANDBOX_DOMAIN'):
            await OpenSandboxBackend().working_dir()

    async def test_unreachable_server_on_attach_is_unavailable(self, fake_opensandbox: FakeOpenSandbox) -> None:
        fake_opensandbox.fail('get_sandbox_info', SandboxConnectionException('Network connectivity error: refused'))
        with pytest.raises(WorkspaceUnavailableError, match='OPEN_SANDBOX_DOMAIN'):
            await OpenSandboxBackend(ref=_ref('sbx-9')).working_dir()

    async def test_auth_failure_is_unavailable_without_echoing_the_key(self, fake_opensandbox: FakeOpenSandbox) -> None:
        fake_opensandbox.fail('create', api_error(401, 'INVALID_API_KEY', 'Authentication credentials are invalid.'))
        with pytest.raises(WorkspaceUnavailableError, match='OPEN_SANDBOX_API_KEY') as raised:
            await OpenSandboxBackend().working_dir()
        assert 'Authentication credentials are invalid' not in str(raised.value)

    async def test_auth_failure_on_attach_is_unavailable(self, fake_opensandbox: FakeOpenSandbox) -> None:
        fake_opensandbox.fail('get_sandbox_info', api_error(401, 'INVALID_API_KEY', 'invalid'))
        with pytest.raises(WorkspaceUnavailableError, match='OPEN_SANDBOX_API_KEY'):
            await OpenSandboxBackend(ref=_ref('sbx-9')).working_dir()

    async def test_image_refused_is_unavailable(self, fake_opensandbox: FakeOpenSandbox) -> None:
        fake_opensandbox.fail(
            'create', api_error(500, 'DOCKER::SANDBOX_IMAGE_PULL_FAILED', 'Failed to pull image nope:1: Not Found')
        )
        with pytest.raises(WorkspaceUnavailableError, match="image 'nope:1'"):
            await OpenSandboxBackend(image='nope:1').working_dir()

    async def test_other_create_failures_keep_the_sdk_error(self, fake_opensandbox: FakeOpenSandbox) -> None:
        refused = api_error(503, 'SERVICE_BUSY', 'busy')
        fake_opensandbox.fail('create', refused)
        with pytest.raises(WorkspaceError, match='Could not start OpenSandbox sandbox'):
            await OpenSandboxBackend().working_dir()

    async def test_create_that_never_answers_times_out(
        self, fake_opensandbox: FakeOpenSandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr('pydantic_ai_opensandbox._backend._CREATE_TIMEOUT', 0.05)
        fake_opensandbox.create_hangs = True
        with pytest.raises(TimeoutError, match='did not complete'):
            await OpenSandboxBackend().working_dir()

    async def test_failed_working_dir_creation_keeps_the_ref(
        self, fake_opensandbox: FakeOpenSandbox, tmp_path: Path
    ) -> None:
        fake_opensandbox.fail('create_directories', api_error(500, 'RUNTIME_ERROR', 'disk full'))
        backend = OpenSandboxBackend(working_dir=str(tmp_path.resolve() / 'project'))
        with pytest.raises(WorkspaceError, match='Could not create working_dir'):
            await backend.working_dir()
        # The sandbox exists, so its ref is there for cleanup, and the next use retries the mkdir.
        assert backend.ref == _ref('sbx-1')
        await backend.working_dir()

    async def test_trio_is_refused(self, fake_opensandbox: FakeOpenSandbox, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr('pydantic_ai_opensandbox._backend.running_on_asyncio', lambda: False)
        with pytest.raises(UserError, match='asyncio'):
            await OpenSandboxBackend().working_dir()


class TestConstruction:
    def test_rejects_other_providers(self) -> None:
        with pytest.raises(ValueError, match="expected 'opensandbox'"):
            OpenSandboxBackend(ref=WorkspaceRef(provider='e2b', id='x'))

    async def test_rejects_sandbox_and_ref_together(self, fake_opensandbox: FakeOpenSandbox) -> None:
        existing = await _existing(fake_opensandbox)
        with pytest.raises(ValueError, match='either `sandbox` or `ref`'):
            OpenSandboxBackend(sandbox=existing, ref=_ref(existing.id))  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize('value', [0, -1, True, 1.5])
    def test_rejects_bad_sandbox_timeout(self, value: int) -> None:
        with pytest.raises(ValueError, match='sandbox_timeout must be an integer of at least 1'):
            OpenSandboxBackend(sandbox_timeout=value)

    def test_rejects_non_string_env(self) -> None:
        with pytest.raises(TypeError, match='env keys and values must be strings'):
            OpenSandboxBackend(env={'A': 1})  # pyright: ignore[reportArgumentType]

    def test_rejects_relative_working_dir(self) -> None:
        with pytest.raises(ValueError, match='working_dir must be an absolute'):
            OpenSandboxBackend(working_dir='project')


class TestRun:
    async def test_exit_code_and_exact_output(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        result = await backend.run(['sh', '-c', "printf 'a\\nb'; printf 'e\\n' >&2; exit 3"])
        # execd's own stream would have dropped the line terminators.
        assert (result.exit_code, result.stdout, result.stderr) == (3, 'a\nb', 'e\n')

    async def test_shell_string_runs_under_sh(self, fake_opensandbox: FakeOpenSandbox) -> None:
        result = await OpenSandboxBackend().run('echo $((1+1))', shell=True)
        assert result.stdout == '2\n'

    async def test_env_layers_on_backend_env(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend(env={'A': '1', 'B': '1'})
        result = await backend.run(['sh', '-c', 'printf "$A$B"'], env={'B': '2'})
        assert result.stdout == '12'
        [(_, opts)] = [call for call in fake_opensandbox.sandboxes['sbx-1'].commands.calls if 'printf' in call[0]]
        assert opts.envs == {'A': '1', 'B': '2'}

    async def test_runs_in_the_working_dir(self, fake_opensandbox: FakeOpenSandbox, tmp_path: Path) -> None:
        project = str(tmp_path.resolve() / 'project')
        backend = OpenSandboxBackend(working_dir=project)
        assert (await backend.run(['pwd', '-P'])).stdout == f'{project}\n'

    async def test_missing_working_dir_on_an_attached_sandbox(
        self, fake_opensandbox: FakeOpenSandbox, tmp_path: Path
    ) -> None:
        existing = await _existing(fake_opensandbox)
        backend = OpenSandboxBackend(ref=_ref(existing.id), working_dir=str(tmp_path.resolve() / 'missing'))
        with pytest.raises(WorkspaceError, match=r"working_dir '.*missing' does not exist"):
            await backend.run(['true'])

    async def test_server_timeout_backstops_the_client_deadline(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        await backend.run(['true'], timeout=5)
        await backend.run(['true'])
        timeouts = [opts.timeout for _, opts in fake_opensandbox.sandboxes['sbx-1'].commands.calls]
        assert timedelta(seconds=35) in timeouts
        assert timeouts[-1] is None

    async def test_timeout_kills_command_and_keeps_partial_output(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        await backend.working_dir()
        commands = fake_opensandbox.sandboxes['sbx-1'].commands
        with pytest.raises(WorkspaceTimeoutError) as raised:
            await backend.run(['sh', '-c', 'echo started; sleep 30'], timeout=0.5)
        assert raised.value.stdout == 'started\n'
        assert commands.interrupts
        await _no_process_left(commands)

    async def test_cancel_kills_command(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        await backend.working_dir()
        commands = fake_opensandbox.sandboxes['sbx-1'].commands
        with anyio.move_on_after(0.5):
            await backend.run(['sleep', '30'])
        assert commands.interrupts
        await _no_process_left(commands)

    async def test_cancel_before_the_command_reports_its_id(self, fake_opensandbox: FakeOpenSandbox) -> None:
        """A start whose init event never arrived is fenced or killed through its claim, not left running."""
        backend = OpenSandboxBackend()
        await backend.working_dir()
        commands = fake_opensandbox.sandboxes['sbx-1'].commands
        commands.withhold_init = True
        with anyio.move_on_after(0.5):
            await backend.run(['sleep', '30'])
        assert commands.interrupts == []
        await _no_process_left(commands)

    async def test_output_limit(self, fake_opensandbox: FakeOpenSandbox, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr('pydantic_ai_opensandbox._backend._MAX_OUTPUT_BYTES', 1000)
        monkeypatch.setattr('pydantic_ai_opensandbox._backend._OUTPUT_PREVIEW_BYTES', 10)
        backend = OpenSandboxBackend()
        with pytest.raises(WorkspaceOutputLimitError) as raised:
            await backend.run(['sh', '-c', 'while :; do echo yyyyyyyyy; done'], timeout=10)
        assert raised.value.stdout == 'yyyyyyyyy\n'
        await _no_process_left(fake_opensandbox.sandboxes['sbx-1'].commands)

    async def test_sandbox_gone_mid_command_is_unavailable(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        await backend.working_dir()

        async def kill_soon() -> None:
            await anyio.sleep(0.2)
            fake_opensandbox.kill('sbx-1')

        async with anyio.create_task_group() as group:
            group.start_soon(kill_soon)
            with pytest.raises(WorkspaceUnavailableError, match='no longer exists'):
                await backend.run(['sleep', '30'])

    async def test_transient_transport_failure_propagates(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        await backend.working_dir()
        dropped = SandboxConnectionException('Network connectivity error: reset')
        fake_opensandbox.fail('run', dropped)
        with pytest.raises(SandboxConnectionException):
            await backend.run(['true'])

    async def test_other_command_errors_are_workspace_errors(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        await backend.working_dir()
        fake_opensandbox.fail('run', api_error(500, 'RUNTIME_ERROR', 'execd broke'))
        with pytest.raises(WorkspaceError, match='execd broke'):
            await backend.run(['true'])

    async def test_capture_files_are_removed(self, fake_opensandbox: FakeOpenSandbox) -> None:
        backend = OpenSandboxBackend()
        await backend.working_dir()
        before = set(Path('/tmp').glob('.pydantic-ai-opensandbox-*'))
        await backend.run(['sh', '-c', 'echo out; echo err >&2'])
        assert set(Path('/tmp').glob('.pydantic-ai-opensandbox-*')) == before


async def _no_process_left(commands: FakeCommands) -> None:
    with anyio.fail_after(5):
        while any(running.process.poll() is None for running in commands.running.values()):
            await anyio.sleep(0.01)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestFilesystem:
    async def test_binary_round_trip(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        data = bytes(range(256)) * 10
        await backend.write_bytes(str(root / 'a' / 'b.bin'), data)
        assert await backend.read_bytes(str(root / 'a' / 'b.bin')) == data

    async def test_new_file_is_not_executable(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        await OpenSandboxBackend().write_bytes(str(root / 'new.txt'), b'x')
        assert _mode(root / 'new.txt') == 0o644

    async def test_write_keeps_an_existing_mode(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        script = root / 'run.sh'
        script.write_bytes(b'#!/bin/sh\n')
        script.chmod(0o750)
        await OpenSandboxBackend().write_bytes(str(script), b'#!/bin/sh\necho hi\n')
        assert (script.read_bytes(), _mode(script)) == (b'#!/bin/sh\necho hi\n', 0o750)

    async def test_write_through_a_symlink_keeps_the_target_mode(
        self, fake_opensandbox: FakeOpenSandbox, root: Path
    ) -> None:
        target = root / 'target.sh'
        target.write_bytes(b'old')
        target.chmod(0o700)
        (root / 'link').symlink_to(target)
        await OpenSandboxBackend().write_bytes(str(root / 'link'), b'new')
        assert (target.read_bytes(), _mode(target)) == (b'new', 0o700)
        assert (root / 'link').is_symlink()

    async def test_read_errors(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        (root / 'dir').mkdir()
        (root / 'dirlink').symlink_to(root / 'dir')
        os.mkfifo(root / 'fifo')
        with pytest.raises(FileNotFoundError):
            await backend.read_bytes(str(root / 'missing'))
        with pytest.raises(IsADirectoryError):
            await backend.read_bytes(str(root / 'dir'))
        with pytest.raises(IsADirectoryError):
            await backend.read_bytes(str(root / 'dirlink'))
        with pytest.raises(OSError, match='FIFO'):
            await backend.read_bytes(str(root / 'fifo'))

    async def test_read_through_a_symlink(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        (root / 'file').write_bytes(b'data')
        (root / 'link').symlink_to('file')
        assert await OpenSandboxBackend().read_bytes(str(root / 'link')) == b'data'

    async def test_write_errors(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        (root / 'dir').mkdir()
        (root / 'file').write_bytes(b'x')
        with pytest.raises(IsADirectoryError):
            await backend.write_bytes(str(root / 'dir'), b'x')
        with pytest.raises(NotADirectoryError):
            await backend.write_bytes(str(root / 'file' / 'child'), b'x')

    async def test_stat(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        (root / 'dir').mkdir()
        (root / 'file').write_bytes(b'four')
        (root / 'dirlink').symlink_to('dir')
        (root / 'dangling').symlink_to('nowhere')
        assert await backend.stat(str(root / 'file')) == FileEntry(
            name='file', path=str(root / 'file'), is_dir=False, size=4
        )
        assert await backend.stat(str(root / 'dir')) == FileEntry(
            name='dir', path=str(root / 'dir'), is_dir=True, size=None
        )
        assert (await backend.stat(str(root / 'dirlink'))).is_dir
        assert await backend.stat(str(root / 'dangling')) == FileEntry(
            name='dangling', path=str(root / 'dangling'), is_dir=False, size=None
        )
        with pytest.raises(FileNotFoundError):
            await backend.stat(str(root / 'missing'))

    async def test_list_dir(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        (root / 'b').mkdir()
        (root / 'a').write_bytes(b'x')
        (root / 'loop').symlink_to('loop')
        entries = await backend.list_dir(str(root))
        assert [(entry.name, entry.path, entry.is_dir) for entry in entries] == [
            ('a', str(root / 'a'), False),
            ('b', str(root / 'b'), True),
            ('loop', str(root / 'loop'), False),
        ]

    async def test_list_dir_through_a_symlink(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        (root / 'real').mkdir()
        (root / 'real' / 'child').write_bytes(b'x')
        (root / 'link').symlink_to('real')
        entries = await OpenSandboxBackend().list_dir(str(root / 'link'))
        assert [(entry.name, entry.path) for entry in entries] == [('child', str(root / 'link' / 'child'))]

    async def test_list_dir_errors(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        (root / 'file').write_bytes(b'x')
        with pytest.raises(NotADirectoryError):
            await backend.list_dir(str(root / 'file'))
        with pytest.raises(FileNotFoundError):
            await backend.list_dir(str(root / 'missing'))

    async def test_make_dir(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        await backend.make_dir(str(root / 'a' / 'b'))
        await backend.make_dir(str(root / 'a' / 'b'))
        assert (root / 'a' / 'b').is_dir()
        (root / 'file').write_bytes(b'x')
        with pytest.raises(FileExistsError):
            await backend.make_dir(str(root / 'file'))
        with pytest.raises(NotADirectoryError):
            await backend.make_dir(str(root / 'file' / 'child'))

    async def test_remove(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend(working_dir=str(root / 'work'))
        (root / 'file').write_bytes(b'x')
        (root / 'tree' / 'nested').mkdir(parents=True)
        (root / 'tree' / 'nested' / 'f').write_bytes(b'x')
        await backend.remove(str(root / 'file'))
        await backend.remove(str(root / 'tree'))
        assert not (root / 'file').exists() and not (root / 'tree').exists()
        with pytest.raises(FileNotFoundError):
            await backend.remove(str(root / 'missing'))

    async def test_remove_a_symlink_removes_only_the_link(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend(working_dir=str(root / 'work'))
        (root / 'dir').mkdir()
        (root / 'dir' / 'keep').write_bytes(b'x')
        (root / 'dirlink').symlink_to('dir')
        (root / 'dangling').symlink_to('nowhere')
        await backend.remove(str(root / 'dirlink'))
        await backend.remove(str(root / 'dangling'))
        assert (root / 'dir' / 'keep').exists()
        assert not (root / 'dirlink').is_symlink() and not (root / 'dangling').is_symlink()

    async def test_remove_refuses_the_working_dir_and_its_ancestors(
        self, fake_opensandbox: FakeOpenSandbox, root: Path
    ) -> None:
        backend = OpenSandboxBackend(working_dir=str(root / 'work'))
        for path in (root / 'work', root):
            with pytest.raises(ValueError, match='cannot remove the workspace root'):
                await backend.remove(str(path))

    async def test_exists(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        (root / 'file').write_bytes(b'x')
        (root / 'dangling').symlink_to('nowhere')
        assert await backend.exists(str(root / 'file'))
        assert await backend.exists(str(root))
        assert not await backend.exists(str(root / 'missing'))
        assert not await backend.exists(str(root / 'dangling'))

    async def test_unexpected_file_api_errors_are_workspace_errors(
        self, fake_opensandbox: FakeOpenSandbox, root: Path
    ) -> None:
        backend = OpenSandboxBackend()
        await backend.working_dir()
        fake_opensandbox.fail('get_file_info', api_error(500, 'RUNTIME_ERROR', 'disk on fire'))
        with pytest.raises(WorkspaceError, match='disk on fire'):
            await backend.stat(str(root))

    async def test_permission_errors_are_builtin(self, fake_opensandbox: FakeOpenSandbox, root: Path) -> None:
        backend = OpenSandboxBackend()
        (root / 'file').write_bytes(b'x')
        await backend.working_dir()
        fake_opensandbox.fail('read_bytes', api_error(500, 'RUNTIME_ERROR', 'open file: permission denied'))
        with pytest.raises(PermissionError):
            await backend.read_bytes(str(root / 'file'))
