"""A controllable fake `opensandbox` SDK for OpenSandbox tests.

Tests never reach a real OpenSandbox server: the `fake_opensandbox` fixture replaces the backend's SDK module with
this fake. Sandboxes act on the host: commands run under `/bin/bash -c` in their own process group and file calls
touch a temporary host directory, so commands and files share one tree, as they do in a real sandbox.

Fidelity to the real SDK and server is the point; the behaviors mirrored here are the ones recorded in
`docs/sdk-notes.md`. Models and exception classes are the real ones from the installed package, every call
suspends, execd's line-split output and its refusal of argv lists are reproduced, a command whose client call is
cancelled keeps running until `interrupt`, and file errors carry the server's status codes and wording.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import signal
import stat
import subprocess
import tempfile
import types
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import IOBase
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxConnectionException,
    SandboxError,
    SandboxReadyTimeoutException,
)
from opensandbox.models.execd import (
    Execution,
    ExecutionComplete,
    ExecutionError,
    ExecutionHandlers,
    ExecutionInit,
    ExecutionLogs,
    OutputMessage,
    RunCommandOpts,
)
from opensandbox.models.filesystem import DirectoryListEntry, EntryInfo, WriteEntry
from opensandbox.models.sandboxes import (
    NetworkPolicy,
    SandboxImageSpec,
    SandboxInfo,
    SandboxRenewResponse,
    SandboxStatus,
)

__all__ = ('CAPTURE_PREFIX', 'FakeCreateCall', 'FakeOpenSandbox', 'FakeSandbox', 'api_error')

# The backend's own scratch files live here, outside the host root the fake otherwise confines writes to.
CAPTURE_PREFIX = '/tmp/.pydantic-ai-opensandbox-'


def api_error(status: int, code: str, message: str) -> SandboxApiException:
    """A `SandboxApiException` shaped like the ones the SDK raises for a server error response."""
    return SandboxApiException(message, status_code=status, error=SandboxError(code, message))


def _gone() -> SandboxConnectionException:
    # A killed sandbox's execd endpoint no longer answers; the SDK reports the refused connection like this.
    return SandboxConnectionException('Network connectivity error: All connection attempts failed')


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


@dataclass(frozen=True)
class FakeCreateCall:
    image: str
    timeout: timedelta | None
    env: dict[str, str] | None
    network_policy: NetworkPolicy | None


@dataclass
class _Running:
    process: subprocess.Popen[bytes]
    out: Any
    err: Any


class FakeCommands:
    """Mirrors `sandbox.commands`: execd running each command under `bash -c` on the host."""

    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sandbox = sandbox
        self.calls: list[tuple[str, RunCommandOpts]] = []
        self.interrupts: list[str] = []
        self.running: dict[str, _Running] = {}

    async def run(
        self, command: str | list[str], *, opts: RunCommandOpts | None = None, handlers: ExecutionHandlers | None = None
    ) -> Execution:
        await self._sandbox.checkpoint('run')
        if not isinstance(command, str):
            # execd v1.1.0 refuses the SDK's argv form.
            raise api_error(400, 'INVALID_REQUEST_BODY', "invalid request, 'Command' failed on the 'required' tag")
        opts = opts or RunCommandOpts()
        self.calls.append((command, opts))
        control = self._sandbox.control
        cwd = opts.working_directory
        if cwd is not None and not Path(cwd).is_dir():
            raise api_error(
                400,
                'INVALID_REQUEST_BODY',
                f'invalid request, validation error working directory does not exist: {cwd}: stat {cwd}: '
                'no such file or directory',
            )
        assert control.host_root is not None
        out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        process = subprocess.Popen(
            ['/bin/bash', '-c', command],
            cwd=cwd or control.host_root,
            env={**os.environ, **self._sandbox.env, **(opts.envs or {})},
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        execution_id = uuid.uuid4().hex
        self.running[execution_id] = _Running(process, out, err)
        if handlers is not None and handlers.on_init is not None:
            await handlers.on_init(ExecutionInit(id=execution_id, timestamp=_now_ms()))
        deadline = None if opts.timeout is None else anyio.current_time() + opts.timeout.total_seconds()
        while (code := process.poll()) is None:
            self._sandbox.check_alive()
            if deadline is not None and anyio.current_time() >= deadline:
                self._signal(execution_id, signal.SIGKILL)
            await anyio.sleep(0.01)
        self._sandbox.check_alive()
        del self.running[execution_id]
        return self._execution(execution_id, -1 if code < 0 else code, out, err)

    async def interrupt(self, execution_id: str) -> None:
        await self._sandbox.checkpoint('interrupt')
        self.interrupts.append(execution_id)
        self._signal(execution_id, signal.SIGTERM)

    def _signal(self, execution_id: str, signum: signal.Signals) -> None:
        if (running := self.running.get(execution_id)) is not None:
            with suppress(ProcessLookupError):
                os.killpg(running.process.pid, signum)

    def close(self) -> None:
        """End every process this sandbox started, as killing the sandbox does."""
        for running in self.running.values():
            with suppress(ProcessLookupError):
                os.killpg(running.process.pid, signal.SIGKILL)
            running.process.wait()
            running.out.close()
            running.err.close()
        self.running.clear()

    @staticmethod
    def _execution(execution_id: str, exit_code: int, out: Any, err: Any) -> Execution:
        def lines(stream: Any) -> list[OutputMessage]:
            stream.seek(0)
            text = stream.read().decode(errors='replace')
            stream.close()
            # execd scans lines and drops their terminators.
            return [OutputMessage(text=line, timestamp=_now_ms()) for line in text.splitlines()]

        logs = ExecutionLogs(stdout=lines(out), stderr=lines(err))
        error = (
            None
            if exit_code == 0
            else ExecutionError(name='CommandExecError', value=str(exit_code), timestamp=_now_ms())
        )
        complete = ExecutionComplete(timestamp=_now_ms(), execution_time_in_millis=0) if exit_code == 0 else None
        return Execution(id=execution_id, exit_code=exit_code, error=error, complete=complete, logs=logs)


def _mode_digits(mode: int) -> int:
    # The server reports permissions as octal digits read as a decimal int: 0o755 is 755.
    return int(oct(stat.S_IMODE(mode))[2:])


def _entry_type(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return 'symlink'
    if stat.S_ISDIR(mode):
        return 'directory'
    if stat.S_ISREG(mode):
        return 'file'
    return 'other'


class FakeFiles:
    """Mirrors `sandbox.files` over the host filesystem."""

    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sandbox = sandbox
        self.writes: list[tuple[str, int]] = []

    def _confine(self, path: str) -> None:
        # File calls act on the developer's disk: keep them under the host root or the backend's scratch files.
        assert posixpath.isabs(path), f'the backend must send absolute paths, got {path!r}'
        normalized = posixpath.normpath(path)
        if normalized.startswith(CAPTURE_PREFIX):
            return
        root = self._sandbox.control.host_root
        assert root is not None
        resolved = Path(os.path.realpath(posixpath.dirname(normalized)), posixpath.basename(normalized))
        assert resolved.is_relative_to(os.path.realpath(root)), f'fake refused {path!r}: outside the host root'

    async def _check(self, operation: str, path: str) -> None:
        await self._sandbox.checkpoint(operation)
        self._confine(path)

    async def read_bytes(
        self, path: str, *, range_header: str | None = None, offset: int | None = None, limit: int | None = None
    ) -> bytes:
        await self._check('read_bytes', path)
        assert range_header is None
        target = Path(path)
        if not target.exists():
            raise api_error(404, 'FILE_NOT_FOUND', f'file not found. open {path}: no such file or directory')
        if target.is_dir():
            # execd drops the connection instead of answering a read of a directory.
            raise SandboxConnectionException('Network connectivity error: peer closed connection')
        assert not stat.S_ISFIFO(target.stat().st_mode), 'the backend must not read a FIFO: execd would block'
        with target.open('rb') as file:
            file.seek(offset or 0)
            return file.read() if limit is None else file.read(limit)

    async def write_file(
        self,
        path: str,
        data: str | bytes | IOBase,
        *,
        encoding: str = 'utf-8',
        mode: int = 755,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        await self._check('write_file', path)
        assert isinstance(data, bytes) and owner is None and group is None
        del encoding
        target = Path(path)
        self._confine(os.path.realpath(path))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            raise api_error(
                500,
                'RUNTIME_ERROR',
                f'error creating target directory {target.parent}. mkdir {target.parent}: not a directory',
            ) from None
        if target.is_dir():
            raise api_error(500, 'RUNTIME_ERROR', f'error opening destination file {path}. open {path}: is a directory')
        target.write_bytes(data)
        # execd applies the requested mode to the file, following a symlink like the write does.
        os.chmod(path, int(str(mode), 8))
        self.writes.append((path, mode))

    async def get_file_info(self, paths: list[str]) -> dict[str, EntryInfo]:
        for path in paths:
            await self._check('get_file_info', path)
        return {path: self._lstat(path) for path in paths}

    async def list_directory(self, entry: DirectoryListEntry) -> list[EntryInfo]:
        path = entry.path
        await self._check('list_directory', path)
        assert entry.depth == 1, f'unexpected list depth {entry.depth!r}'
        info = self._lstat(path)
        if info.entry_type == 'symlink':
            raise api_error(400, 'INVALID_REQUEST_BODY', f'path is a symbolic link, refusing to traverse: {path}')
        if info.entry_type != 'directory':
            raise api_error(400, 'INVALID_REQUEST_BODY', f'path is not a directory: {path}')
        return [self._lstat(posixpath.join(path, name)) for name in sorted(os.listdir(path))]

    async def create_directories(self, entries: list[WriteEntry]) -> None:
        for entry in entries:
            await self._check('create_directories', entry.path)
            try:
                Path(entry.path).mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                raise api_error(
                    500, 'RUNTIME_ERROR', f'error accessing file: mkdir {entry.path}: not a directory'
                ) from None

    async def delete_files(self, paths: list[str]) -> None:
        for path in paths:
            await self._check('delete_files', path)
            if Path(path).is_dir():
                # Checked through a symlink, as execd does.
                raise api_error(500, 'RUNTIME_ERROR', f'error removing file {path}. path is a directory: {path}')
            Path(path).unlink(missing_ok=True)

    async def delete_directories(self, paths: list[str]) -> None:
        for path in paths:
            await self._check('delete_directories', path)
            # Go's `os.RemoveAll`: recursive, a missing path is fine, a link goes itself.
            if Path(path).is_dir() and not Path(path).is_symlink():
                shutil.rmtree(path)
            else:
                Path(path).unlink(missing_ok=True)

    @staticmethod
    def _lstat(path: str) -> EntryInfo:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise api_error(
                404,
                'FILE_NOT_FOUND',
                f'file not found. file not found: {path}: lstat {path}: no such file or directory',
            ) from None
        timestamp = datetime.fromtimestamp(info.st_mtime, UTC)
        return EntryInfo.model_validate(
            {
                'path': path,
                'type': _entry_type(info.st_mode),
                'mode': _mode_digits(info.st_mode),
                'owner': 'root',
                'group': 'root',
                'size': info.st_size,
                'modified_at': timestamp,
                'created_at': timestamp,
            }
        )


class FakeSandbox:
    """Mirrors `opensandbox.Sandbox` for the members the backend uses."""

    def __init__(self, control: FakeOpenSandbox, sandbox_id: str, env: dict[str, str] | None = None) -> None:
        self.control = control
        self.id = sandbox_id
        self.env = dict(env or {})
        self.state = 'Running'
        self.commands = FakeCommands(self)
        self.files = FakeFiles(self)
        self.renewals: list[timedelta] = []
        self.closed = 0

    async def checkpoint(self, operation: str) -> None:
        await anyio.lowlevel.checkpoint()
        self.check_alive()
        self.control.raise_injected(operation)

    def check_alive(self) -> None:
        if self.state != 'Running':
            raise _gone()

    def info(self) -> SandboxInfo:
        return SandboxInfo(
            id=self.id,
            status=SandboxStatus(state=self.state),
            entrypoint=['tail', '-f', '/dev/null'],
            image=SandboxImageSpec(image='fake'),
            metadata={},
            created_at=datetime.now(timezone.utc),
        )

    async def get_info(self) -> SandboxInfo:
        await self.checkpoint('get_info')
        return self.info()

    async def renew(self, timeout: timedelta) -> SandboxRenewResponse:
        await self.checkpoint('renew')
        self.renewals.append(timeout)
        return SandboxRenewResponse(expires_at=datetime.now(UTC) + timeout)

    async def kill(self) -> None:
        await anyio.lowlevel.checkpoint()
        self.control.kill(self.id)

    async def close(self) -> None:
        self.closed += 1


class _SandboxClass:
    """Mirrors the `Sandbox.create` / `connect` / `resume` class methods."""

    def __init__(self, control: FakeOpenSandbox) -> None:
        self._control = control

    async def create(
        self,
        image: str,
        *,
        timeout: timedelta | None,
        ready_timeout: timedelta,
        env: dict[str, str] | None = None,
        network_policy: NetworkPolicy | None = None,
    ) -> FakeSandbox:
        del ready_timeout
        control = self._control
        control.create_calls.append(FakeCreateCall(image, timeout, env, network_policy))
        await anyio.lowlevel.checkpoint()
        control.raise_injected('create')
        if control.create_hangs:
            await anyio.sleep_forever()
        sandbox = FakeSandbox(control, f'sbx-{len(control.sandboxes) + 1}', env)
        control.sandboxes[sandbox.id] = sandbox
        return sandbox

    async def connect(self, sandbox_id: str, *, connect_timeout: timedelta) -> FakeSandbox:
        del connect_timeout
        control = self._control
        control.connect_calls.append(sandbox_id)
        await anyio.lowlevel.checkpoint()
        control.raise_injected('connect')
        sandbox = control.lookup(sandbox_id)
        if sandbox.state == 'Paused':
            # A paused sandbox never passes the SDK's health check.
            raise SandboxReadyTimeoutException('Sandbox health check timed out')
        return sandbox

    async def resume(self, sandbox_id: str, *, resume_timeout: timedelta) -> FakeSandbox:
        del resume_timeout
        control = self._control
        control.resume_calls.append(sandbox_id)
        await anyio.lowlevel.checkpoint()
        control.raise_injected('resume')
        sandbox = control.lookup(sandbox_id)
        sandbox.state = 'Running'
        return sandbox


class FakeManager:
    """Mirrors `opensandbox.SandboxManager`: control-plane calls by sandbox id."""

    def __init__(self, control: FakeOpenSandbox) -> None:
        self._control = control
        self.closed = False

    async def get_sandbox_info(self, sandbox_id: str) -> SandboxInfo:
        await anyio.lowlevel.checkpoint()
        self._control.raise_injected('get_sandbox_info')
        return self._control.lookup(sandbox_id).info()

    async def kill_sandbox(self, sandbox_id: str) -> None:
        await anyio.lowlevel.checkpoint()
        self._control.raise_injected('kill_sandbox')
        self._control.lookup(sandbox_id)
        self._control.kill(sandbox_id)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeManager:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


class _ManagerClass:
    def __init__(self, control: FakeOpenSandbox) -> None:
        self._control = control

    async def create(self) -> FakeManager:
        await anyio.lowlevel.checkpoint()
        manager = FakeManager(self._control)
        self._control.managers.append(manager)
        return manager


@dataclass
class FakeOpenSandbox:
    """Control surface for the injected fake `opensandbox` module."""

    host_root: Path | None = None
    sandboxes: dict[str, FakeSandbox] = field(default_factory=dict[str, FakeSandbox])
    create_calls: list[FakeCreateCall] = field(default_factory=list[FakeCreateCall])
    connect_calls: list[str] = field(default_factory=list[str])
    resume_calls: list[str] = field(default_factory=list[str])
    managers: list[FakeManager] = field(default_factory=list[FakeManager])
    create_hangs: bool = False
    # One-shot failures by operation name (`create`, `connect`, `run`, `read_bytes`, ...).
    injected: dict[str, list[Exception]] = field(default_factory=dict[str, list[Exception]])

    def __post_init__(self) -> None:
        self.module = types.ModuleType('opensandbox')
        self.module.Sandbox = _SandboxClass(self)  # type: ignore[attr-defined]
        self.module.SandboxManager = _ManagerClass(self)  # type: ignore[attr-defined]

    def fail(self, operation: str, error: Exception, *, times: int = 1) -> None:
        """Make the next `times` calls of `operation` raise `error`."""
        self.injected.setdefault(operation, []).extend([error] * times)

    def raise_injected(self, operation: str) -> None:
        if errors := self.injected.get(operation):
            raise errors.pop(0)

    def lookup(self, sandbox_id: str) -> FakeSandbox:
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None or sandbox.state == 'Killed':
            raise api_error(404, 'DOCKER::SANDBOX_NOT_FOUND', f'Sandbox {sandbox_id} not found.')
        return sandbox

    def kill(self, sandbox_id: str) -> None:
        sandbox = self.sandboxes[sandbox_id]
        sandbox.state = 'Killed'
        sandbox.commands.close()

    def close(self) -> None:
        """Reap every host process a test left running."""
        for sandbox in self.sandboxes.values():
            sandbox.commands.close()

    def pause(self, sandbox_id: str) -> None:
        """Pause a sandbox, as `Sandbox.pause` or an idle policy would."""
        self.lookup(sandbox_id).state = 'Paused'
