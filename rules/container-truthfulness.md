---
name: container-truthfulness
description: MANDATORY when config stack.runtime != "none". Two container states that present as healthy — orphaned in-container processes left by a killed `exec` client (and the missing-`ps` false negative that hides them), and half-up stacks after a host restart. Includes the /proc check and recovery order.
---
# Containers Lie About Being Healthy

Applies when `config: stack.runtime` is not `"none"`. Companion to `live-sync.md` (file sync); this
covers **process and service state**.

## 1. Killing an `exec` client does not kill the process inside the container
A cancelled or timed-out `{compose} -p <stack> exec -T <service> <test command>` can leave the process
running inside the container (always on Windows hosts; possible elsewhere), still holding resources
such as the test database.

**How it presents:** every later run in that stack fails at DB setup/teardown with "database already
exists" / "is being accessed by other users".

**Terminating DB connections alone does not fix it** — the orphan reconnects. Kill the *process*, not
the *connection*.

**`ps` may not be installed in the image.** `exec ... ps | grep <x>` then errors, greps nothing, and
reads as "no orphan". Use `/proc`, which is always present:
```bash
{compose} -p <stack> exec -T <service> sh -c \
  'for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); \
   case "$c" in *<pattern>*) echo "${p#/proc/}: $c";; esac; done'
```

**Recovery, in order:**
1. Identify the orphan (above).
2. Kill it, or `{compose} -p <stack> restart <service>`.
3. Only then terminate remaining DB sessions on the test database.
4. Verify the session count is 0 **before** dropping — a drop against a live session just fails again.

**Before blaming anything external, check for your own orphan first.**

## 2. A host restart leaves a stack half-up
After a host restart, containers stop (`Exited (255)`, not OOM). Services with a restart policy come
back; the rest do not. `{compose} ps` **without `-a`** lists only the running ones and reads as a live
stack.

- **Always use `-a` when diagnosing:**
  `{compose} -p <stack> ps -a --format "{{.Service}}\t{{.State}}\t{{.Status}}"`
- **Recover with `start`, not `up`:** `{compose} -p <stack> start <services...>`. For the shared stack
  use `uv run --no-project .workflow/scripts/start_slot0.py`, which does this with every port var and
  waits until the app service runs. `up` can recreate
  containers and needs every port env var (`config: stack.ports`); `start` cannot remap ports.
- **`compose watch` does not survive a host restart** (it is a foreground process). After any reboot:
  restart the stack → verify sync (`.workflow/scripts/check_container_sync.py`) before trusting any
  test result → restart `compose watch` in the worktree you are working in.
- `.workflow/scripts/stack_preflight.py` gates on all `config: stack.services` running and catches all
  of this — run it before any test pass.

## The generalisation
The first diagnostic you reach for can return a false negative indistinguishable from a clean result.
When a stack misbehaves for reasons the application cannot explain, **verify the instrument before
trusting its reading.**
