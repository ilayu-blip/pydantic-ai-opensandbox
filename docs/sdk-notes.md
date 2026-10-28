# OpenSandbox SDK behavior, verified live

Probed 2026-10-28 with `scripts/probe_sdk.py` against `opensandbox` 1.2.0 (Python SDK),
`opensandbox-server` (Docker runtime, `uvx`), `opensandbox/execd:v1.1.0`, `opensandbox/egress:v1.1.7`,
image `python:3.12`, Docker Desktop on macOS (arm64). Server config: `~/.sandbox.toml` from
`opensandbox-server init-config --example docker`, with `[proxy] resolve_internal = false` (macOS
cannot route to bridge IPs) and an `api_key`.

## Default image

`DEFAULT_IMAGE = 'python:3.12'` (Debian 13): bash, sh, setsid, git, python3, curl, readlink, stat,
find, pkill, base64, mktemp. No ripgrep (same as E2B's default template). `opensandbox/code-interpreter`
was rejected as a default: 10.3 GB.

## Lifecycle

- `Sandbox.create(image, timeout=timedelta, env=..., network_policy=...)` → ~0.5–1 s with the image
  cached. `sandbox.id` is a UUID string. Default user is root, `HOME=/root`, `pwd -P` is `/` for
  `python:3.12` (no WORKDIR).
- `Sandbox.connect(id)` on a running sandbox → works, no new sandbox.
- `Sandbox.connect(id)` on a **paused** sandbox → `SandboxReadyTimeoutException` after
  `connect_timeout` (health check never passes). `Sandbox.resume(id)` resumes and returns a handle.
  So attach must `get_info` first (via `SandboxManager` or a cheap call) or catch and resume — check state
  `SandboxInfo.status.state == 'Paused'`.
- `connect` on a killed or never-existing id → `SandboxApiException`, `status_code == 404`,
  `error.code == 'DOCKER::SANDBOX_NOT_FOUND'` (the code prefix is runtime-specific; match on
  `status_code == 404` plus `SANDBOX_NOT_FOUND` suffix).
- Running a command on a handle whose sandbox was killed → `SandboxConnectionException`
  ("All connection attempts failed").
- `sandbox.renew(timedelta)` → `SandboxRenewResponse(expires_at=...)`.
- `sandbox.kill()` → deletes it.
- Bad image → `SandboxApiException`, `status_code` 500-ish with `error.code ==
  'DOCKER::SANDBOX_IMAGE_PULL_FAILED'`.
- Wrong API key → `SandboxApiException`, `status_code == 401`, `error.code == 'INVALID_API_KEY'`.
- Unreachable domain → `SandboxConnectionException` ("All connection attempts failed").

## Commands

- **argv lists are refused** by execd v1.1.0 (`400 INVALID_REQUEST_BODY ... 'Command' required`), even
  though SDK 1.2.0 accepts `list[str]`. Send one string: execd runs it under `bash -c`
  (`$0 == 'bash'`). So quote argv with `shlex.join`, as E2B does.
- **Every command takes ~1.0 s** because execd sleeps `EXECD_API_GRACE_SHUTDOWN` (1 s in execd
  v1.1.0) before closing the SSE stream. Creating the sandbox with env
  `EXECD_API_GRACE_SHUTDOWN=0s` drops it to ~3 ms. It only delays the stream close; exit status
  arrives earlier.
- **Streamed output is lossy**: execd splits stdout/stderr into lines, drops terminators, and decodes
  as UTF-8 with replacement (`printf 'a\nb'` → `['a', 'b']`, `\xff` → `�`). For exact bytes, redirect
  inside the command (`exec >OUT 2>ERR; <command>`) and read the files with `files.read_bytes`
  (verified: `x\0y` and `err\n` round-trip exactly; exit code preserved).
- Exit code: `Execution.exit_code` (non-zero also sets `Execution.error` with `name='CommandExecError'`);
  signal-killed → `-1`. A missing program → 127 via bash.
- stdin is closed (`cat` returns immediately).
- `RunCommandOpts(envs=...)` layers on top of create-time `env` (overrides same keys).
- `RunCommandOpts(working_directory=...)` must exist, otherwise `SandboxApiException` 400
  `INVALID_REQUEST_BODY` "working directory does not exist".
- `RunCommandOpts(timeout=...)`: server kills the command (`signal: killed`, exit `-1`) and returns
  output so far.
- `ExecutionHandlers(on_init=...)` delivers `ExecutionInit(id=...)` right after start.
  `commands.interrupt(id)` terminates the whole process group (a backgrounded `(sleep &)` child died
  too), and the running `commands.run` call returns with exit `-1`.
- **Cancelling the client call does not stop the command** (a `sleep 200` kept running). The backend
  must call `interrupt(id)` on timeout/cancellation.

## Files

- `files.write_file(path, bytes)` creates parent directories. Binary round trip through
  `read_bytes` is exact.
- `read_bytes` missing → `SandboxApiException` 404, `error.code == 'FILE_NOT_FOUND'`.
  On a directory → `SandboxConnectionException` (server drops the connection): stat first.
- `get_file_info([...])` → `dict[path, EntryInfo]`; `entry_type` in `file`, `directory`, `symlink`,
  `other` (FIFO); it does **not** follow symlinks (lstat). Missing → 404 `FILE_NOT_FOUND`.
  `mode` is reported as decimal-looking octal digits (`755`, `644`, `777`).
- `list_directory(DirectoryListEntry(path=...))` → `list[EntryInfo]` of direct children
  (includes symlink loops as `symlink`). Missing → 404.
- `create_directories([WriteEntry(path=...)])` is `mkdir -p`.
- `delete_files` on a directory → `SandboxApiException` "path is a directory";
  `delete_directories` removes a tree; `delete_files` on a missing path succeeds silently.
- Writing onto a directory → `SandboxApiException` 500 "is a directory"; writing under a file →
  500 "not a directory". Translate from the message text, as E2B's `_path_error` does.
- Reads ignore file permissions (root): `chmod 000` file is readable.
- Relative paths are accepted (resolved by execd's cwd); the backend always sends absolute paths.

## Network

`network_policy=NetworkPolicy(default_action='deny')` (needs `[docker] network_mode = "bridge"` and
the egress sidecar image) → outbound HTTPS fails inside the sandbox; without a policy it returns 200.
Create with the policy takes ~1.8 s (sidecar).
