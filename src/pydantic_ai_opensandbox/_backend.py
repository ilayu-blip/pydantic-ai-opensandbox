"""An OpenSandbox sandbox behind Pydantic AI's `WorkspaceBackend` protocol.

SDK and server assumptions verified 2026-10-28 against `opensandbox` 1.2.0, `opensandbox-server` with its Docker
runtime, and `opensandbox/execd:v1.1.0` (details and probe script in the repository's `docs/sdk-notes.md`):

- execd refuses the SDK's argv-list command form, so commands are sent as one string, which execd runs under
  `bash -c`. It streams output split into lines with their terminators dropped and decoded with replacement, so
  output is redirected to files in the sandbox and read back with the file API for exact bytes.
- execd holds every command stream open for `EXECD_API_GRACE_SHUTDOWN` (1 s in v1.1.0) after the command exits.
  A sandbox this backend creates gets `0s`, which brings a short command from about 1 s to a few milliseconds.
- Cancelling the SDK's `commands.run` leaves the command running; `commands.interrupt(id)` ends its process
  group. The id arrives in the stream's init event.
- `Sandbox.connect` cannot attach to a paused sandbox (its health check never passes); `Sandbox.resume` can.
- Server errors are `SandboxApiException` with an HTTP `status_code` and a code (`FILE_NOT_FOUND`,
  `INVALID_API_KEY`, `DOCKER::SANDBOX_NOT_FOUND`, ...); transport failures are `SandboxConnectionException`.
- The SDK runs on asyncio only (`asyncio.gather` and asyncio locks).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

import anyio
import anyio.lowlevel
import opensandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException, SandboxConnectionException, SandboxException
from opensandbox.models.execd import Execution, ExecutionHandlers, ExecutionInit, RunCommandOpts
from opensandbox.models.filesystem import WriteEntry
from opensandbox.models.sandboxes import NetworkPolicy
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    CommandResult,
    SupportsCommands,
    WorkspaceBackend,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from ._workspace_provider import (
    absolute_path,
    check_env_types,
    check_timeout,
    command_argv,
    running_on_asyncio,
    safe_credential_reason,
    stop_shielded,
)

if TYPE_CHECKING:
    from pydantic_ai.workspaces import WorkspaceCommand

__all__ = ('DEFAULT_IMAGE', 'DEFAULT_SANDBOX_TIMEOUT', 'OpenSandboxBackend', 'kill_sandbox')

logger = logging.getLogger(__name__)

PROVIDER = 'opensandbox'

DEFAULT_IMAGE = 'python:3.12'
"""Has bash, git, Python, curl and util-linux; not ripgrep."""

DEFAULT_SANDBOX_TIMEOUT = 3_600

# Bounds the create call, which on a cold server includes pulling the image.
_CREATE_TIMEOUT = 300
# The SDK's budget for a new or attached sandbox's endpoint and health check.
_READY_TIMEOUT = timedelta(seconds=60)

# Bounds the backend's own short commands (`pwd -P` and the stop script).
_INTERNAL_EXEC_TIMEOUT = 10

# The server's own command timeout, past the client deadline, ends a command whose client went away.
_SERVER_TIMEOUT_MARGIN = 30

# The local backend's cap and preview.
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
_OUTPUT_PREVIEW_BYTES = 64 * 1024
_OUTPUT_POLL_INTERVAL = 0.25

# Each command's claim directory, holding its pid and output, under the sandbox's `/tmp`.
_CLAIM_PREFIX = '/tmp/.pydantic-ai-opensandbox-'

# execd's per-command stream close delay; see the module docstring.
_FAST_STREAM_ENV = {'EXECD_API_GRACE_SHUTDOWN': '0s'}

_AUTH_MESSAGE = 'OpenSandbox rejected the API key. Set a valid OPEN_SANDBOX_API_KEY in the environment.'

# The server's and Go's errno wording, most specific first: "is not a directory" must not match "is a directory".
_PATH_ERRORS: tuple[tuple[str, type[OSError]], ...] = (
    ('already exists', FileExistsError),
    ('file exists', FileExistsError),
    ('permission denied', PermissionError),
    ('not a directory', NotADirectoryError),
    ('is a directory', IsADirectoryError),
)


def _require_asyncio() -> None:
    """Refuse an operation off asyncio before it reaches the OpenSandbox SDK, which needs a running asyncio loop."""
    if not running_on_asyncio():
        raise UserError('OpenSandbox needs the asyncio event loop: the OpenSandbox SDK runs every request on asyncio.')


def _auth_error(error: Exception) -> WorkspaceUnavailableError:
    return WorkspaceUnavailableError(f'{safe_credential_reason(error)}. {_AUTH_MESSAGE}')


def _unreachable_error() -> WorkspaceUnavailableError:
    domain = ConnectionConfig().get_domain()
    return WorkspaceUnavailableError(
        f'Could not reach the OpenSandbox server at {domain!r}. Set OPEN_SANDBOX_DOMAIN to its address.'
    )


def _gone_message(sandbox_id: str, state: str | None = None) -> str:
    if state is not None:
        return (
            f'The OpenSandbox sandbox {sandbox_id!r} is {state.lower()}, not running. '
            'A later run that attaches to a paused sandbox resumes it.'
        )
    return (
        f'The OpenSandbox sandbox {sandbox_id!r} no longer exists: it was killed or its `sandbox_timeout` ran out. '
        "Pass `workspace='new'` to start a fresh sandbox."
    )


def _is_auth_refusal(error: SandboxApiException) -> bool:
    return error.status_code in (401, 403)


def _is_sandbox_not_found(error: SandboxApiException) -> bool:
    return error.status_code == 404 and error.error.code.endswith('SANDBOX_NOT_FOUND')


def _path_error(error: Exception, path: str) -> OSError | None:
    """The builtin path error a server failure describes, or `None` if it is not one."""
    message = str(error).lower()
    for marker, builtin in _PATH_ERRORS:
        if marker in message:
            return builtin(f'{error} (in the OpenSandbox sandbox: {path!r})')
    return None


def _not_found(path: str | None) -> FileNotFoundError:
    return FileNotFoundError(f'No such file or directory in the OpenSandbox sandbox: {path!r}')


async def _new_manager() -> opensandbox.SandboxManager:
    return await opensandbox.SandboxManager.create()


class OpenSandboxBackend(WorkspaceBackend, SupportsCommands):
    """An [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) sandbox as a Pydantic AI `WorkspaceBackend`.

    Commands and file operations run inside a sandbox container on an OpenSandbox server, not on the host. The
    server's address and API key come from the `OPEN_SANDBOX_DOMAIN` and `OPEN_SANDBOX_API_KEY` environment
    variables, which the OpenSandbox SDK reads.

    Building one does no I/O. The first operation creates or attaches to a sandbox, and the typed
    `opensandbox.Sandbox` is available through `get_sandbox()`. The backend does not kill the sandbox; killing it
    is the application's job.

    Commands run as one-shot operations, with complete output returned after they finish. Every command runs
    through execd's `bash -c`, so an argv sequence is quoted into a single shell word string first; a
    `shell=True` string runs under `/bin/sh -c` inside it. Output goes to files under `/tmp` in the sandbox, so the
    image needs a writable `/tmp`. On timeout or cancellation the command's process group is interrupted.

    The OpenSandbox SDK runs every request on asyncio, so under Trio every operation raises `UserError` before it
    reaches the server.

    Args:
        sandbox: A live `opensandbox.Sandbox` you already have. Whoever created it owns killing it.
        ref: Identity of an existing sandbox to attach to on first use.
        image: Container image a newly created sandbox runs. An image the server cannot pull raises
            `WorkspaceUnavailableError` on first use. It needs `/bin/sh` and `/bin/bash`.
        allow_internet_access: Whether a newly created sandbox may reach the internet. `False` sends a deny-all
            egress policy, which the server enforces with its egress sidecar.
        sandbox_timeout: Total lifetime of the sandbox in seconds, applied when it is created and renewed when
            attaching to it. When it runs out, the server deletes the sandbox.
        working_dir: Absolute directory commands start in and relative paths resolve against. Created on a new
            sandbox and applied per command, including on an attached sandbox; `None` uses the sandbox's own
            default, discovered with `pwd -P` on first use.
        env: Environment variables every command gets, on a created or an attached sandbox; per-command `env` is
            layered on top. Nothing is read from the host environment.
    """

    def __init__(
        self,
        *,
        sandbox: opensandbox.Sandbox | None = None,
        ref: WorkspaceRef | None = None,
        image: str = DEFAULT_IMAGE,
        allow_internet_access: bool = True,
        sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if ref is not None and ref.provider != PROVIDER:
            raise ValueError(f'unsupported workspace provider {ref.provider!r}; expected {PROVIDER!r}')
        if sandbox is not None and ref is not None:
            raise ValueError('pass either `sandbox` or `ref`, not both')
        check_env_types(env)
        if type(sandbox_timeout) is not int or sandbox_timeout < 1:
            raise ValueError(f'sandbox_timeout must be an integer of at least 1, got {sandbox_timeout!r}.')
        self._ref = ref if sandbox is None else WorkspaceRef(provider=PROVIDER, id=sandbox.id)
        self._sandbox = sandbox
        self._working_dir = absolute_path('working_dir', working_dir)
        # `working_dir()` must return a canonical absolute path, resolved once with `pwd -P`.
        self._resolved_working_dir: str | None = None
        self._lock = anyio.Lock()
        # A sandbox this backend created still needs its `working_dir` made.
        self._working_dir_pending = False
        self._acquisition: asyncio.Task[opensandbox.Sandbox] | None = None
        self._image = image
        self._allow_internet_access = allow_internet_access
        self._sandbox_timeout = sandbox_timeout
        self._env = dict(env) if env is not None else None

    async def get_sandbox(self) -> opensandbox.Sandbox:
        """Return the typed `opensandbox.Sandbox`, for OpenSandbox features the workspace API does not cover.

        On a backend with no sandbox yet, this creates one or attaches to the one `ref` names, just like the first
        operation; attaching resumes a paused sandbox. Attaching by `ref` to a sandbox that no longer exists raises
        `WorkspaceUnavailableError`; it does not create a replacement. After that it returns the cached handle
        without checking that the sandbox still exists.
        """
        if self._ref is None and running_on_asyncio():
            # An AnyIO shield cannot stop native Task.cancel(); the backend owns this task until it has recorded
            # the new sandbox, even if every caller leaves.
            task = self._acquisition
            if task is None:
                task = asyncio.create_task(self._acquire_detached(), name='opensandbox-acquisition')
                # Waiting callers still get the failure; this only stops asyncio reporting it as never retrieved
                # when every caller was cancelled first.
                task.add_done_callback(lambda done: done.cancelled() or done.exception())
                self._acquisition = task
            await asyncio.wait([task])
            return task.result()
        return await self._acquire()

    async def _sandbox_for_operation(self) -> opensandbox.Sandbox:
        _require_asyncio()
        return await self.get_sandbox()

    async def _acquire_detached(self) -> opensandbox.Sandbox:
        try:
            return await self._acquire()
        finally:
            self._acquisition = None

    async def _acquire(self) -> opensandbox.Sandbox:
        async with self._lock:
            if (sandbox := self._sandbox) is None:
                _require_asyncio()
                ref = self._ref
                sandbox = await self._attach(ref.id) if ref is not None else await self._create()
                if ref is None:
                    # Recorded at creation, so the id can be found whatever ends the run before it is stored.
                    logger.info('Created OpenSandbox sandbox %s', sandbox.id)
                # Record the new sandbox before setup, so a failed mkdir still leaves a ref for cleanup.
                self._sandbox = sandbox
                self._ref = WorkspaceRef(provider=PROVIDER, id=sandbox.id)
                self._working_dir_pending = ref is None and self._working_dir is not None
            if self._working_dir_pending:
                assert self._working_dir is not None
                # Retried on every acquisition until it succeeds.
                with anyio.CancelScope(shield=True):
                    async with self._sdk_errors(sandbox.id, 'Could not create working_dir', self._working_dir):
                        await sandbox.files.create_directories([WriteEntry(path=self._working_dir)])
                self._working_dir_pending = False
        await anyio.lowlevel.checkpoint_if_cancelled()
        return sandbox

    @property
    def ref(self) -> WorkspaceRef | None:
        """Identity of the sandbox, or `None` before one has been created."""
        return self._ref

    async def _create(self) -> opensandbox.Sandbox:
        """Start a fresh sandbox.

        The acquisition task survives caller cancellation and the shield keeps this call running too;
        `_CREATE_TIMEOUT` bounds it. Rejected credentials, an image the server cannot pull, and an unreachable
        server are `WorkspaceUnavailableError`: retrying the same request cannot succeed.
        """
        context = 'Could not start OpenSandbox sandbox'
        with anyio.move_on_after(_CREATE_TIMEOUT, shield=True):
            async with self._acquisition_errors(None, context):
                try:
                    return await opensandbox.Sandbox.create(
                        self._image,
                        timeout=timedelta(seconds=self._sandbox_timeout),
                        ready_timeout=_READY_TIMEOUT,
                        env={**_FAST_STREAM_ENV, **(self._env or {})},
                        network_policy=None if self._allow_internet_access else NetworkPolicy(defaultAction='deny'),
                    )
                except SandboxApiException as error:
                    if not error.error.code.endswith('IMAGE_PULL_FAILED'):
                        raise
                    raise WorkspaceUnavailableError(
                        f'{context}: the server could not pull image {self._image!r}: {error}'
                    ) from error
        raise TimeoutError(
            f'OpenSandbox sandbox creation did not complete within {_CREATE_TIMEOUT}s; '
            'the server may be unreachable or still pulling the image.'
        )

    async def _attach(self, sandbox_id: str) -> opensandbox.Sandbox:
        """Attach to a sandbox that already exists, resuming it if it is paused, and renew its lifetime.

        A sandbox that is gone raises `WorkspaceUnavailableError`; nothing is created in its place, so a run that
        expected files there is told they are gone instead of being handed an empty workspace.
        """
        async with self._acquisition_errors(sandbox_id, f'Could not connect to OpenSandbox sandbox {sandbox_id!r}'):
            manager = await _new_manager()
            try:
                info = await manager.get_sandbox_info(sandbox_id)
            finally:
                await manager.close()
            if info.status.state == 'Paused':
                sandbox = await opensandbox.Sandbox.resume(sandbox_id, resume_timeout=_READY_TIMEOUT)
            else:
                sandbox = await opensandbox.Sandbox.connect(sandbox_id, connect_timeout=_READY_TIMEOUT)
            await sandbox.renew(timedelta(seconds=self._sandbox_timeout))
            return sandbox

    @asynccontextmanager
    async def _acquisition_errors(self, sandbox_id: str | None, context: str) -> AsyncGenerator[None]:
        """`_sdk_errors` for creating or attaching, where an unreachable server ends the run."""
        try:
            yield
        except SandboxConnectionException as error:
            raise _unreachable_error() from error
        except Exception as error:
            translated = await self._translate(error, context, None, sandbox_id)
            if translated is error:
                raise
            raise translated from error

    @asynccontextmanager
    async def _sdk_errors(self, sandbox_id: str | None, context: str, path: str | None = None) -> AsyncGenerator[None]:
        """Raise the SDK's exceptions as the protocol's typed failures; see `_translate`."""
        try:
            yield
        except Exception as error:
            translated = await self._translate(error, context, path, sandbox_id)
            if translated is error:
                raise
            raise translated from error

    async def _translate(self, error: Exception, context: str, path: str | None, sandbox_id: str | None) -> Exception:
        """Map one SDK exception onto the protocol's typed failures, or return it unchanged.

        Rejected credentials and a gone sandbox end the run. A transport failure on a sandbox that the control
        plane no longer reports as running is unavailable; otherwise it comes back unchanged, to propagate as
        transient for durable engines to retry, as do rate limits. Other server errors mean the operation failed.
        """
        if isinstance(error, SandboxApiException):
            if error.status_code == 400 and 'working directory does not exist' in str(error):
                # Only `run` passes a working directory, and it is always the configured `working_dir`: typically
                # an attached sandbox that never had it, since a created one gets it made.
                return WorkspaceError(
                    f'working_dir {self._working_dir!r} does not exist in OpenSandbox sandbox {sandbox_id}. '
                    'Create it there, or pass a working_dir that exists.'
                )
            if _is_auth_refusal(error):
                return _auth_error(error)
            if _is_sandbox_not_found(error) and sandbox_id is not None:
                return WorkspaceUnavailableError(_gone_message(sandbox_id))
            if error.status_code == 404 and error.error.code == 'FILE_NOT_FOUND':
                return _not_found(path)
            if path is not None and (path_error := _path_error(error, path)) is not None:
                return path_error
            if error.status_code == 429:
                return error
        elif isinstance(error, SandboxConnectionException) and sandbox_id is not None:
            if (state := await _state(sandbox_id)) == 'Missing':
                return WorkspaceUnavailableError(_gone_message(sandbox_id))
            if state is not None and state != 'Running':
                return WorkspaceUnavailableError(_gone_message(sandbox_id, state))
            return error
        if isinstance(error, SandboxException) and not isinstance(error, SandboxConnectionException):
            return WorkspaceError(f'{context}: {error}')
        return error

    async def working_dir(self) -> str:
        """The canonical absolute directory commands start in."""
        if self._resolved_working_dir is None:
            result = await self._probe(['pwd', '-P'], 'Could not determine the working directory')
            printed = result.stdout.removesuffix('\n')
            if result.exit_code != 0 or not printed.startswith('/'):
                raise WorkspaceError(
                    f'Could not determine the working directory of OpenSandbox sandbox {self._ref}: '
                    f'`pwd -P` exited {result.exit_code} and printed {result.stdout!r}.'
                )
            self._resolved_working_dir = printed
        return self._resolved_working_dir

    async def _probe(self, argv: list[str], context: str) -> CommandResult:
        """Run one of the backend's own short commands; `run` already translates SDK failures."""
        try:
            return await self.run(argv, timeout=_INTERNAL_EXEC_TIMEOUT)
        except WorkspaceTimeoutError as error:
            raise WorkspaceTimeoutError(
                f'{context}: the OpenSandbox sandbox did not answer within {_INTERNAL_EXEC_TIMEOUT}s',
                stdout=error.stdout,
                stderr=error.stderr,
            ) from error

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command, interrupting it on timeout, cancellation, an output overflow, or a failed result read."""
        line = shlex.join(command_argv(command, shell))
        check_timeout(timeout)
        # Acquiring the sandbox has its own bound; the timeout is the command's alone.
        sandbox = await self._sandbox_for_operation()
        launch = _Launch(sandbox, line)
        opts = RunCommandOpts(
            working_directory=self._working_dir,
            envs={**(self._env or {}), **(env or {})},
            timeout=None if timeout is None else timedelta(seconds=timeout + _SERVER_TIMEOUT_MARGIN),
        )
        stopped = False
        try:
            execution, overflow = await launch.execute(opts, timeout)
            if execution is None:
                await stop_shielded(launch.stop)
                stopped = True
                stdout = await self._read_output(sandbox, launch.out, _OUTPUT_PREVIEW_BYTES)
                stderr = await self._read_output(sandbox, launch.err, _OUTPUT_PREVIEW_BYTES)
                if overflow:
                    raise WorkspaceOutputLimitError(
                        'OpenSandbox command output exceeded the 10 MiB limit; '
                        'redirect it to a file and read part of it instead',
                        limit=_MAX_OUTPUT_BYTES,
                        stdout=stdout,
                        stderr=stderr,
                    )
                assert timeout is not None
                raise WorkspaceTimeoutError(
                    f'Command timed out after {timeout:g} seconds', stdout=stdout, stderr=stderr
                )
            if execution.exit_code is None:
                # The stream ended without an exit status: execd or the sandbox went away mid-command.
                raise SandboxConnectionException('The command stream ended without an exit status')
            stdout = await self._read_output(sandbox, launch.out)
            stderr = await self._read_output(sandbox, launch.err)
            return CommandResult(exit_code=execution.exit_code, stdout=stdout, stderr=stderr)
        except BaseException as error:
            if not stopped:
                await stop_shielded(launch.stop, grace=_INTERNAL_EXEC_TIMEOUT)
            if isinstance(error, Exception) and not isinstance(error, WorkspaceError):
                context = (
                    'Command could not run in the OpenSandbox sandbox'
                    if launch.execution_id is None
                    else 'Could not read the command result (the command may still be running)'
                )
                translated = await self._translate(error, context, None, sandbox.id)
                if translated is not error:
                    raise translated from error
            raise
        finally:
            await launch.clean_up()

    async def _read_output(self, sandbox: opensandbox.Sandbox, path: str, limit: int | None = None) -> str:
        async with self._sdk_errors(sandbox.id, 'Could not read the command output'):
            try:
                data = await sandbox.files.read_bytes(path, limit=limit)
            except SandboxApiException as error:
                if error.status_code != 404:
                    raise
                return ''  # A command fenced or stopped before it redirected its output.
        return data.decode(errors='replace')


class _Launch:
    """One command's launch in a sandbox: its claim directory, its execd id, and how to stop it.

    The claim directory holds the launcher's pid and the command's output. The launcher and a stop race on its
    atomic `mkdir`, so a stop that runs before the command started fences it, and one that runs after finds the
    process group to end. `$$` is the shell execd started, which leads the command's process group.
    """

    def __init__(self, sandbox: opensandbox.Sandbox, line: str) -> None:
        self._sandbox = sandbox
        self._claim = f'{_CLAIM_PREFIX}{uuid.uuid4().hex}'
        self.out = f'{self._claim}/out'
        self.err = f'{self._claim}/err'
        self._script = (
            f'mkdir {self._claim} 2>/dev/null || exit 143; echo $$ > {self._claim}/pid; '
            f'exec >{self.out} 2>{self.err}; {line}'
        )
        self.execution_id: str | None = None
        self._completed = False

    async def _on_init(self, init: ExecutionInit) -> None:
        self.execution_id = init.id

    async def execute(self, opts: RunCommandOpts, timeout: float | None) -> tuple[Execution | None, bool]:
        """Run the command; `None` with `True` when its output overflowed, `None` with `False` when it timed out."""
        overflow = False
        execution: Execution | None = None
        failure: Exception | None = None

        async def watch_output(scope: anyio.CancelScope) -> None:
            nonlocal overflow
            while True:
                await anyio.sleep(_OUTPUT_POLL_INTERVAL)
                try:
                    sizes = await self._sandbox.files.get_file_info([self.out, self.err])
                except SandboxException:
                    continue  # Not created yet.
                if sum(entry.size for entry in sizes.values()) > _MAX_OUTPUT_BYTES:
                    overflow = True
                    scope.cancel()
                    return

        with anyio.move_on_after(timeout):
            async with anyio.create_task_group() as group:
                group.start_soon(watch_output, group.cancel_scope)
                try:
                    execution = await self._sandbox.commands.run(
                        self._script, opts=opts, handlers=ExecutionHandlers(on_init=self._on_init)
                    )
                except Exception as error:  # noqa: BLE001 - re-raised below, outside the task group's exception group
                    failure = error
                group.cancel_scope.cancel()
        if failure is not None:
            raise failure
        self._completed = execution is not None
        return execution, overflow

    async def stop(self) -> None:
        if self.execution_id is not None:
            await self._sandbox.commands.interrupt(self.execution_id)
            return
        # The start's init event never arrived: fence the launcher, or end the group it registered.
        claim = self._claim
        await self._sandbox.commands.run(
            f'if mkdir {claim} 2>/dev/null; then exit 0; fi; '
            f'i=0; while [ ! -s {claim}/pid ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i+1)); done; '
            f'p=$(cat {claim}/pid 2>/dev/null) && kill -KILL -- "-$p" 2>/dev/null; rm -rf {claim}; true',
            opts=RunCommandOpts(timeout=timedelta(seconds=_INTERNAL_EXEC_TIMEOUT)),
        )

    async def clean_up(self) -> None:
        """Remove the claim once the command is known to have started; one a stop took fences a late launcher."""
        if self.execution_id is None and not self._completed:
            return
        with anyio.move_on_after(1, shield=True):
            try:
                await self._sandbox.files.delete_directories([self._claim])
            except Exception:
                logger.debug('Could not remove OpenSandbox command claim %s', self._claim, exc_info=True)


async def _state(sandbox_id: str) -> str | None:
    """The control plane's state for a sandbox, `'Missing'` once it is deleted, or `None` if the probe fails.

    Probed only after a transport failure, which keeps the extra round trip off successful operations; a probe that
    fails leaves the original error to propagate rather than ending the run on a guess.
    """
    try:
        manager = await _new_manager()
        try:
            info = await manager.get_sandbox_info(sandbox_id)
        finally:
            await manager.close()
    except SandboxApiException as error:
        return 'Missing' if _is_sandbox_not_found(error) else None
    except Exception:
        logger.debug('OpenSandbox state probe for sandbox %s failed', sandbox_id, exc_info=True)
        return None
    return info.status.state


async def kill_sandbox(sandbox_id: str) -> None:
    """Kill a sandbox by ID without connecting, which would resume a paused one.

    A sandbox that is already gone returns quietly; rejected credentials raise `WorkspaceUnavailableError`.
    """
    _require_asyncio()
    manager = await _new_manager()
    try:
        await manager.kill_sandbox(sandbox_id)
    except SandboxApiException as error:
        if _is_auth_refusal(error):
            raise _auth_error(error) from error
        if not _is_sandbox_not_found(error):
            raise
    finally:
        await manager.close()
