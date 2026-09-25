---
name: host-passive-environment
description: MANDATORY when config env.app_runs_in_container = true. App-side commands (runtime, package managers, test runners) run only inside the app container; the host runs tooling only. Covers forbidden host installs, the sanctioned host-Python paths, and container-path quoting on Windows.
---
# Host-Passive Environment

Applies when `config: env.app_runs_in_container` is `true` (default).

## Rule
**App-side commands never run on the host.** The app's runtime, its package managers, and its test
runners run via `{compose} -p <stack> exec -T <service> ...` (`config: stack.app_service` for the
app; other services as the project defines). If the container toolchain fails, fix the container —
never fall back to host execution.

Hook-enforced: `.workflow/hooks/guards/bash_command.py` denies host commands matching
`config: guards.host_forbidden`.

## Host: tooling only
- **Standalone tooling scripts and hooks** (stdlib-only or PEP 723 inline deps, no project imports):
  `config: env.host_tooling_runner` (default `uv run --no-project <script>`). Kit scripts also run as
  `python3 <script>`. If PEP 723 dependency resolution fails with a TLS `UnknownIssuer` error, add
  `--system-certs`.
- **Scripts importing project code or project dependencies:** `config: env.project_python` only. If
  it is empty, such scripts run in the container.
- **MCP server runtimes** launched by the agent harness (e.g. `npx`/`uvx` entries in the MCP config)
  are tooling, not app-side, and are allowed. This does not loosen the app-side ban.
- ❌ Bare `python`/`python3` for project-importing scripts; a project-importing script under
  `uv run --no-project` (its deps are absent from the ephemeral env).

## Forbidden installs
- ❌ **Host:** any installing command — `npm install`, `pip install`, `uv pip install`, `uv sync`,
  `uv add`/`uv remove` without `--no-sync`, `poetry install`, `gem install`, `go get`, etc.
- ❌ **Container, lockfile-managed ecosystems:** ad-hoc `pip install` / `pip freeze` (drifts from the
  lockfile, which is the source of truth).
- ✅ **Host, config-editor mode (edits manifests, installs nothing):** the commands in
  `config: env.dependency_edit_cmds`. Then follow `dependency-sync.md`.
- ✅ **Container:** `config: env.container_install_cmd` for container-managed ecosystems.

## Read source locally, not through the container
Inspect source with the local read/grep tools against the worktree the stack was brought up from
(the shared stack's checkout, or the slot's worktree from `config: paths.slot_registry`). Use
`exec` only for runtime state with no local equivalent (logs, health, process state) or when you
suspect the container is out of sync — and then diagnose the sync (`live-sync.md`) rather than
treating the container as the source of truth.

## Windows note
Git Bash rewrites bare `/...` arguments to Windows paths before launching a `.exe`. When passing a
container-internal path as an **argument** to `docker`/`docker compose exec`, prefix it with `//`:
✅ `exec -T backend ls //app/src` ❌ `exec -T backend ls /app/src`.
Paths inside a quoted script that the container interprets are unaffected. In PowerShell, `&&`/`||`
are parser errors on older versions — chain with `;`; in bash use POSIX syntax.
