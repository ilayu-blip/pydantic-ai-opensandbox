"""Probe a live OpenSandbox server and print how the SDK behaves; results go in docs/sdk-notes.md.

Usage: OPEN_SANDBOX_DOMAIN=localhost:8080 OPEN_SANDBOX_API_KEY=... uv run python scripts/probe_sdk.py [image]
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import ExecutionHandlers, OutputMessage, RunCommandOpts
from opensandbox.models.filesystem import WriteEntry
from opensandbox.models.sandboxes import NetworkPolicy


async def probe(label: str, call: Callable[[], Awaitable[Any]]) -> Any:
    start = time.monotonic()
    try:
        result = await call()
    except Exception as error:  # noqa: BLE001
        print(f'[{label}] RAISED {type(error).__module__}.{type(error).__qualname__}: {error!r}'[:600])
        mro = [c.__name__ for c in type(error).__mro__]
        print(f'    mro={mro} attrs={ {k: v for k, v in vars(error).items() if k != "args"} }'[:600])
        return None
    print(f'[{label}] ({time.monotonic() - start:.2f}s) -> {result!r}'[:800])
    return result


def summary(execution: Any) -> dict[str, Any]:
    return {
        'id': execution.id,
        'exit_code': execution.exit_code,
        'error': execution.error,
        'stdout': [m.text for m in execution.logs.stdout],
        'stderr': [m.text for m in execution.logs.stderr],
    }


async def run(sandbox: Sandbox, command: str | list[str], **opts: Any) -> dict[str, Any]:
    line = command if isinstance(command, str) else shlex.join(command)
    return summary(await sandbox.commands.run(line, opts=RunCommandOpts(**opts)))


async def main(image: str) -> None:
    print(f'image={image}')
    sandbox: Sandbox = await probe(
        'create',
        lambda: Sandbox.create(image, timeout=timedelta(seconds=600), env={'FROM_CREATE': 'yes'}),
    )
    sid = sandbox.id
    print('sandbox id', sid, 'type', type(sid))
    await probe('get_info', sandbox.get_info)

    print('--- commands')
    await probe('argv exit 3', lambda: run(sandbox, ['sh', '-c', 'echo out; echo err >&2; exit 3']))
    await probe('shell text', lambda: run(sandbox, 'echo $((1+1)); echo $0; echo $SHELL'))
    await probe('argv no-newline', lambda: run(sandbox, ['printf', 'a\\nb']))
    await probe('empty output', lambda: run(sandbox, ['true']))
    await probe('missing program', lambda: run(sandbox, ['no-such-program-xyz']))
    await probe('pwd/whoami/home', lambda: run(sandbox, ['sh', '-c', 'pwd -P; whoami; id; echo $HOME']))
    await probe('create env', lambda: run(sandbox, ['sh', '-c', 'echo "$FROM_CREATE"']))
    await probe('envs', lambda: run(sandbox, ['sh', '-c', 'echo "$A $B $FROM_CREATE"'], envs={'A': '1', 'B': 'x y'}))
    await probe(
        'envs override create', lambda: run(sandbox, ['sh', '-c', 'echo "$FROM_CREATE"'], envs={'FROM_CREATE': 'over'})
    )
    await probe('workdir missing', lambda: run(sandbox, ['pwd'], working_directory='/nope/x'))
    await probe('workdir /tmp', lambda: run(sandbox, ['pwd'], working_directory='/tmp'))
    await probe(
        'tools',
        lambda: run(sandbox, ['sh', '-c', 'command -v bash setsid git rg python3 curl readlink stat find; uname -a']),
    )
    await probe('binary stdout', lambda: run(sandbox, ['sh', '-c', 'printf "\\377\\376ok"']))
    await probe('big stdout', lambda: run(sandbox, ['sh', '-c', 'head -c 2000000 /dev/zero | tr "\\0" a; echo']))
    await probe('stdin closed?', lambda: run(sandbox, ['sh', '-c', 'cat; echo done'], timeout=timedelta(seconds=5)))

    print('--- server timeout')
    r = await probe(
        'server timeout 1s',
        lambda: run(sandbox, ['sh', '-c', 'echo started; sleep 30; echo never'], timeout=timedelta(seconds=1)),
    )
    await probe('ps after timeout', lambda: run(sandbox, ['sh', '-c', 'ps -eo pid,pgid,cmd 2>/dev/null || ls /proc']))
    del r

    print('--- interrupt via handlers + cancellation')
    seen: dict[str, Any] = {}

    async def on_init(init: Any) -> None:
        seen['init'] = init

    async def on_stdout(msg: OutputMessage) -> None:
        seen.setdefault('stdout', []).append(msg.text)

    handlers = ExecutionHandlers(on_init=on_init, on_stdout=on_stdout)
    task = asyncio.create_task(
        sandbox.commands.run(
            "sh -c 'echo started; (sleep 300 &) ; sleep 300'", opts=RunCommandOpts(), handlers=handlers
        )
    )
    await asyncio.sleep(2)
    print('seen so far', seen)
    exec_id = seen['init'].id if 'init' in seen else ''
    await probe('status running', lambda: sandbox.commands.get_command_status(exec_id))
    await probe('interrupt', lambda: sandbox.commands.interrupt(exec_id))
    await asyncio.sleep(1)
    print('task done?', task.done())
    if task.done():
        await probe('task result', lambda: task)
    else:
        task.cancel()
    await probe('status after interrupt', lambda: sandbox.commands.get_command_status(exec_id))
    await probe(
        'sleeps left', lambda: run(sandbox, ['sh', '-c', 'ps -eo pid,pgid,cmd | grep "sleep 300" | grep -v grep'])
    )

    print('--- client cancel without interrupt')
    seen.clear()
    task = asyncio.create_task(sandbox.commands.run('sleep 200', opts=RunCommandOpts(), handlers=handlers))
    await asyncio.sleep(1.5)
    task.cancel()
    await asyncio.sleep(1)
    await probe(
        'sleep 200 after client cancel',
        lambda: run(sandbox, ['sh', '-c', 'ps -eo pid,cmd | grep "sleep 200" | grep -v grep']),
    )

    print('--- files')
    data = bytes(range(256)) * 4
    await probe('write_file bytes nested', lambda: sandbox.files.write_file('/tmp/p/q/bin.dat', data))
    got = await probe('read_bytes', lambda: sandbox.files.read_bytes('/tmp/p/q/bin.dat'))
    print('binary round trip ok:', got == data)
    await probe('read missing', lambda: sandbox.files.read_bytes('/tmp/nope.txt'))
    await probe('read dir', lambda: sandbox.files.read_bytes('/tmp/p'))
    await probe('info file+dir', lambda: sandbox.files.get_file_info(['/tmp/p/q/bin.dat', '/tmp/p']))
    await probe('info missing', lambda: sandbox.files.get_file_info(['/tmp/nope.txt']))
    await run(
        sandbox, ['sh', '-c', 'ln -sf /tmp/p/q/bin.dat /tmp/p/link; ln -sf /tmp/p/loop /tmp/p/loop; mkfifo /tmp/p/fifo']
    )
    await probe('info symlink', lambda: sandbox.files.get_file_info(['/tmp/p/link']))
    from opensandbox.models.filesystem import DirectoryListEntry, SearchEntry

    await probe('list_directory', lambda: sandbox.files.list_directory(DirectoryListEntry(path='/tmp/p')))
    await probe('list missing', lambda: sandbox.files.list_directory(DirectoryListEntry(path='/tmp/nope')))
    await probe('search', lambda: sandbox.files.search(SearchEntry(path='/tmp/p', pattern='*')))
    await probe('create_directories', lambda: sandbox.files.create_directories([WriteEntry(path='/tmp/d1/d2')]))
    await probe('delete_files on dir', lambda: sandbox.files.delete_files(['/tmp/d1']))
    await probe('delete_directories', lambda: sandbox.files.delete_directories(['/tmp/d1']))
    await probe('delete_files missing', lambda: sandbox.files.delete_files(['/tmp/nope.txt']))
    await probe('write onto dir', lambda: sandbox.files.write_file('/tmp/p', b'x'))
    await probe('write under file', lambda: sandbox.files.write_file('/tmp/p/q/bin.dat/x', b'x'))
    await run(sandbox, ['sh', '-c', 'echo secret > /tmp/locked; chmod 000 /tmp/locked'])
    await probe('read chmod 000', lambda: sandbox.files.read_bytes('/tmp/locked'))
    await probe('write relative', lambda: sandbox.files.write_file('rel.txt', b'x'))

    print('--- lifecycle')
    await probe('renew', lambda: sandbox.renew(timedelta(seconds=900)))
    other: Sandbox | None = await probe('connect by id', lambda: Sandbox.connect(sid))
    if other is not None:
        await probe('connected run', lambda: run(other, ['echo', 'hi']))
    await probe('pause', sandbox.pause)
    await probe('get_info paused', sandbox.get_info)
    resumed: Sandbox | None = await probe(
        'connect paused', lambda: Sandbox.connect(sid, connect_timeout=timedelta(seconds=10))
    )
    resumed = await probe('resume', lambda: Sandbox.resume(sid))
    if resumed is not None:
        await probe('resumed run', lambda: run(resumed, ['cat', '/tmp/p/q/bin.dat']))
    await probe('kill', sandbox.kill)
    await probe('connect killed', lambda: Sandbox.connect(sid, connect_timeout=timedelta(seconds=10)))
    await probe('run on killed handle', lambda: run(sandbox, ['echo', 'hi']))
    await probe(
        'connect never-existed', lambda: Sandbox.connect('does-not-exist-123', connect_timeout=timedelta(seconds=10))
    )

    print('--- errors')
    await probe('bad image', lambda: Sandbox.create('no-such-image-xyz:nope', ready_timeout=timedelta(seconds=60)))
    await probe(
        'bad api key',
        lambda: Sandbox.create(image, connection_config=ConnectionConfig(api_key='wrong')),
    )
    await probe(
        'bad domain',
        lambda: Sandbox.create(image, connection_config=ConnectionConfig(domain='localhost:1')),
    )

    print('--- egress deny')
    denied: Sandbox | None = await probe(
        'create deny',
        lambda: Sandbox.create(
            image, timeout=timedelta(seconds=300), network_policy=NetworkPolicy(default_action='deny')
        ),
    )
    if denied is not None:
        await probe(
            'python fetch denied',
            lambda: run(
                denied,
                [
                    'python3',
                    '-c',
                    'import urllib.request;print(urllib.request.urlopen("https://example.com",timeout=8).status)',
                ],
            ),
        )
        await denied.kill()
    allowed: Sandbox | None = await probe('create allow', lambda: Sandbox.create(image, timeout=timedelta(seconds=300)))
    if allowed is not None:
        await probe(
            'python fetch allowed',
            lambda: run(
                allowed,
                [
                    'python3',
                    '-c',
                    'import urllib.request;print(urllib.request.urlopen("https://example.com",timeout=8).status)',
                ],
            ),
        )
        await allowed.kill()


if __name__ == '__main__':
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'python:3.12'))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
