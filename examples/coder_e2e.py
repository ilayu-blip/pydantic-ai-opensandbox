"""Run a real agent with `Coder` in an OpenSandbox sandbox, then check its work from outside the agent.

Usage:
    OPEN_SANDBOX_DOMAIN=localhost:8080 OPEN_SANDBOX_API_KEY=... uv run python examples/coder_e2e.py [model]

`model` defaults to `anthropic:claude-opus-5-5`; any Pydantic AI model string works, such as a `bedrock:` one.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic_ai import Agent
from pydantic_ai.workspaces import Workspace
from pydantic_ai_harness.coder import Coder

from pydantic_ai_opensandbox import OpenSandbox, OpenSandboxBackend

PROMPT = (
    'Create /root/e2e/fib.py that prints the first 10 Fibonacci numbers, starting 0 1, space separated on one line. '
    'Run it with python3 and tell me exactly what it printed.'
)


async def main(model: str) -> None:
    capability = OpenSandbox(working_dir='/root/e2e')
    agent = Agent(model, capabilities=[capability, Coder()])
    result = await agent.run(PROMPT)
    ref = result.workspace.ref
    assert ref is not None
    print(f'agent output: {result.output}')
    print(f'workspace ref: {ref}')
    try:
        # Attach a separate backend, as a later run or another process would, and check the agent's work.
        workspace = Workspace(OpenSandboxBackend(ref=ref, working_dir='/root/e2e'))
        print(f'fib.py:\n{await workspace.read_text("fib.py")}')
        run = await workspace.run(['python3', 'fib.py'])
        print(f'python3 fib.py -> exit {run.exit_code}: {run.stdout.strip()}')
        assert run.stdout.split() == ['0', '1', '1', '2', '3', '5', '8', '13', '21', '34']
        print('OK: the file exists in the sandbox and prints the right numbers')
    finally:
        await capability.destroy(ref)
        print(f'destroyed {ref.id}')


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'anthropic:claude-opus-5-5'))
