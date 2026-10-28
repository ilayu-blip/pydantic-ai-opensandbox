"""Public `OpenSandbox` capability tests, driven through `Agent(capabilities=[...])` over the fake SDK."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from opensandbox.models.sandboxes import NetworkPolicy
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import Workspace, WorkspaceRef, WorkspaceUnavailableError
from pydantic_ai_harness.coder import Coder

from pydantic_ai_opensandbox import OpenSandbox, OpenSandboxBackend

from .fake_opensandbox import FakeOpenSandbox, FakeSandbox, api_error

models.ALLOW_MODEL_REQUESTS = False


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _existing(fake: FakeOpenSandbox) -> FakeSandbox:
    sandbox = await fake.module.Sandbox.create('python:3.12', timeout=None, ready_timeout=timedelta(seconds=1))
    assert isinstance(sandbox, FakeSandbox)
    return sandbox


def test_env_is_not_shown_in_repr() -> None:
    secret = 'sensitive-credential-value'
    assert secret not in repr(OpenSandbox(env={'TOKEN': secret}))
    assert secret not in repr(OpenSandboxBackend(env={'TOKEN': secret}))


def test_package_exports() -> None:
    import pydantic_ai_opensandbox

    assert {'OpenSandbox', 'OpenSandboxBackend'} <= set(pydantic_ai_opensandbox.__all__)


@pytest.mark.parametrize('env', [{'PORT': 3000}, {1: 'one'}, {'TOKEN': None}])
def test_non_string_env_fails_at_construction(env: dict[object, object]) -> None:
    with pytest.raises(UserError, match=r'^`env` keys and values must be strings\.$'):
        OpenSandbox(env=env)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ('settings', 'message'),
    [
        ({'working_dir': 'repo'}, "working_dir must be an absolute POSIX path or None, got 'repo'"),
        ({'sandbox_timeout': 0}, 'sandbox_timeout must be an integer of at least 1, got 0'),
        ({'sandbox_timeout': 1.5}, 'sandbox_timeout must be an integer of at least 1, got 1.5'),
        (
            {'defer_loading': True, 'id': 'opensandbox'},
            (
                '`OpenSandbox` does not support `defer_loading=True`: '
                'the workspace is selected before deferred capabilities load.'
            ),
        ),
    ],
)
def test_invalid_settings_fail_at_construction(settings: dict[str, object], message: str) -> None:
    with pytest.raises(UserError, match=re.escape(message)):
        OpenSandbox(**settings)  # pyright: ignore[reportArgumentType]


def test_get_workspace_builds_a_backend_without_io(fake_opensandbox: FakeOpenSandbox) -> None:
    backend = OpenSandbox().get_workspace(_ctx(), ref=None)
    assert isinstance(backend, OpenSandboxBackend)
    assert backend.ref is None
    assert not fake_opensandbox.create_calls


def test_get_workspace_attaches_to_its_own_ref() -> None:
    ref = WorkspaceRef(provider='opensandbox', id='known')
    backend = OpenSandbox().get_workspace(_ctx(), ref=ref)
    assert isinstance(backend, OpenSandboxBackend)
    assert backend.ref == ref


def test_get_workspace_declines_a_foreign_ref() -> None:
    assert OpenSandbox().get_workspace(_ctx(), ref=WorkspaceRef(provider='e2b', id='other')) is None


async def test_foreign_reference_is_rejected(fake_opensandbox: FakeOpenSandbox) -> None:
    foreign = WorkspaceRef(provider='e2b', id='other')
    message = re.escape("unsupported workspace provider 'e2b'; expected 'opensandbox'")
    with pytest.raises(ValueError, match=message):
        OpenSandbox().backend(foreign)
    with pytest.raises(ValueError, match=message):
        await OpenSandbox().destroy(foreign)
    assert not fake_opensandbox.sandboxes


async def test_destroy_kills_without_attaching(fake_opensandbox: FakeOpenSandbox) -> None:
    existing = await _existing(fake_opensandbox)
    await OpenSandbox().destroy(WorkspaceRef(provider='opensandbox', id=existing.id))
    assert existing.state == 'Killed'
    assert fake_opensandbox.connect_calls == []
    assert all(manager.closed for manager in fake_opensandbox.managers)


async def test_destroy_is_idempotent(fake_opensandbox: FakeOpenSandbox) -> None:
    await OpenSandbox().destroy(WorkspaceRef(provider='opensandbox', id='never-existed'))


async def test_destroy_maps_rejected_credentials(fake_opensandbox: FakeOpenSandbox) -> None:
    fake_opensandbox.fail('kill_sandbox', api_error(401, 'INVALID_API_KEY', 'invalid'))
    with pytest.raises(WorkspaceUnavailableError, match='OPEN_SANDBOX_API_KEY'):
        await OpenSandbox().destroy(WorkspaceRef(provider='opensandbox', id='sbx-1'))


async def test_destroy_keeps_other_server_errors(fake_opensandbox: FakeOpenSandbox) -> None:
    busy = api_error(503, 'SERVICE_BUSY', 'busy')
    fake_opensandbox.fail('kill_sandbox', busy)
    with pytest.raises(type(busy), match='busy'):
        await OpenSandbox().destroy(WorkspaceRef(provider='opensandbox', id='sbx-1'))


async def test_agent_without_workspace_use_does_not_create(fake_opensandbox: FakeOpenSandbox) -> None:
    agent = Agent(TestModel(custom_output_text='done'), capabilities=[OpenSandbox()])
    result = await agent.run('hello')
    assert result.output == 'done'
    assert not fake_opensandbox.create_calls


async def test_capability_forwards_creation_options_and_run_does_not_kill(
    fake_opensandbox: FakeOpenSandbox, tmp_path: Path
) -> None:
    project = str(tmp_path.resolve() / 'work')
    capability = OpenSandbox(
        image='python:3.13',
        sandbox_timeout=120,
        working_dir=project,
        env={'FOO': 'bar'},
        allow_internet_access=False,
    )
    agent = Agent(TestModel(call_tools=['run_command']), capabilities=[capability])

    @agent.tool
    async def run_command(ctx: RunContext[object]) -> str:
        return (await ctx.workspace.run(['sh', '-c', 'printf "$FOO"; pwd -P'])).stdout

    result = await agent.run('run')
    assert f'bar{project}' in result.output
    [call] = fake_opensandbox.create_calls
    assert (call.image, call.timeout, call.network_policy) == (
        'python:3.13',
        timedelta(seconds=120),
        NetworkPolicy(defaultAction='deny'),
    )
    assert call.env == {'EXECD_API_GRACE_SHUTDOWN': '0s', 'FOO': 'bar'}
    assert fake_opensandbox.sandboxes['sbx-1'].state == 'Running'
    assert isinstance(result.workspace, Workspace)
    assert result.workspace.ref == WorkspaceRef(provider='opensandbox', id='sbx-1')


async def test_agent_runs_without_history_create_fresh_workspaces(fake_opensandbox: FakeOpenSandbox) -> None:
    agent = Agent(TestModel(call_tools=['run_command']), capabilities=[OpenSandbox()])

    @agent.tool
    async def run_command(ctx: RunContext[object]) -> str:
        return (await ctx.workspace.run(['printf', 'ok'])).stdout

    await agent.run('go')
    await agent.run('go')
    assert len(fake_opensandbox.sandboxes) == 2


async def test_agent_history_attaches_same_workspace(fake_opensandbox: FakeOpenSandbox) -> None:
    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        if isinstance(request, ModelRequest) and any(isinstance(part, ToolReturnPart) for part in request.parts):
            return ModelResponse(parts=[TextPart(content='done')])
        return ModelResponse(parts=[ToolCallPart(tool_name='marker', args={}, tool_call_id='call')])

    agent = Agent(FunctionModel(model), capabilities=[OpenSandbox(sandbox_timeout=600)])
    seen: list[str] = []

    @agent.tool
    async def marker(ctx: RunContext[object]) -> str:
        if await ctx.workspace.exists('marker.txt'):
            seen.append(await ctx.workspace.read_text('marker.txt'))
        else:
            await ctx.workspace.write_text('marker.txt', 'persisted')
            seen.append('created')
        return seen[-1]

    first = await agent.run('go')
    await agent.run('go', message_history=first.all_messages())
    assert seen == ['created', 'persisted']
    assert len(fake_opensandbox.sandboxes) == 1
    assert fake_opensandbox.connect_calls == ['sbx-1']
    assert fake_opensandbox.sandboxes['sbx-1'].renewals == [timedelta(seconds=600)]


async def test_coder_works_in_the_sandbox(fake_opensandbox: FakeOpenSandbox, tmp_path: Path) -> None:
    turn = 0
    calls = [('shell', {'command': 'echo made-in-sandbox > note.txt'}), ('read_file', {'path': 'note.txt'})]
    returned: list[str] = []

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        nonlocal turn
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                returned.append(str(part.content))
        turn += 1
        if turn <= len(calls):
            name, args = calls[turn - 1]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart('done')])

    agent = Agent(FunctionModel(model), capabilities=[OpenSandbox(), Coder(repo_context=False)])
    await agent.run('Use the tools')
    assert (tmp_path / 'note.txt').read_text() == 'made-in-sandbox\n'
    assert 'made-in-sandbox' in returned[-1]
    assert len(fake_opensandbox.sandboxes) == 1


@pytest.mark.parametrize(
    'operation',
    [
        'run',
        'read_bytes',
        'write_bytes',
        'stat',
        'list_dir',
        'make_dir',
        'remove',
        'exists',
        'working_dir',
        'get_sandbox',
        'attach',
        'destroy',
    ],
)
async def test_every_operation_needs_asyncio(
    fake_opensandbox: FakeOpenSandbox, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    # The OpenSandbox SDK runs on asyncio only, so Trio gets a clear error before anything reaches the server.
    monkeypatch.setattr('pydantic_ai_opensandbox._backend.running_on_asyncio', lambda: False)
    backend = OpenSandboxBackend()
    ref = WorkspaceRef(provider='opensandbox', id='sbx-1')
    operations: dict[str, Callable[[], Awaitable[object]]] = {
        'run': lambda: backend.run(['true']),
        'read_bytes': lambda: backend.read_bytes('/file'),
        'write_bytes': lambda: backend.write_bytes('/file', b'data'),
        'stat': lambda: backend.stat('/file'),
        'list_dir': lambda: backend.list_dir('/'),
        'make_dir': lambda: backend.make_dir('/dir'),
        'remove': lambda: backend.remove('/file'),
        'exists': lambda: backend.exists('/file'),
        'working_dir': backend.working_dir,
        'get_sandbox': backend.get_sandbox,
        'attach': OpenSandboxBackend(ref=ref).get_sandbox,
        'destroy': lambda: OpenSandbox().destroy(ref),
    }
    with pytest.raises(UserError, match='OpenSandbox needs the asyncio event loop'):
        await operations[operation]()
    assert not fake_opensandbox.create_calls
    assert not fake_opensandbox.connect_calls
