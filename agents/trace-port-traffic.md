---
name: trace-port-traffic
description: Identify what is generating mystery HTTP traffic to a compose service port. Captures the request fingerprint (method, path, User-Agent, body) with tcpdump inside the target container, then traces the source process (host process lookup, or cross-container /proc lookup). Takes service (required), internal_port (required), project_name (optional), capture_packets (optional), capture_timeout (optional). Use when logs show repeated unexpected requests of unknown origin.
mode: subagent
tier: standard
effort: high
tools: [read, grep, glob, bash]
readonly: false
---

# Trace Port Traffic

**Goal:** given a compose service that is receiving unexpected HTTP requests, identify (a) the
request fingerprint (method, path, User-Agent, body) and (b) the source process — on the host or
inside another container — with its full command line and parent ancestry.

**Right tool when:** a service log shows repeated unexpected requests of unknown origin — repeated
4xx, repeated probes to one endpoint, traffic with no obvious cause in the application code.

**Not the right tool when:** the source is already obvious from the User-Agent in the access log,
or it is clearly the app's own frontend doing something expected.

This agent requires `config: stack.runtime` other than `none`. If it is `none`, stop and report
that there is no container stack to trace.

## Inputs

- **`service`** — required. The compose service where the requests land.
- **`internal_port`** — required. The port the service listens on **inside** the container (not
  the host-published port).
- **`project_name`** — optional. Compose project name of the stack. Default: the shared stack
  (`config: slots.shared_stack_name`); an isolated story stack is `config: slots.stack_prefix` +
  story id.
- **`capture_packets`** — optional. Push-flag packets to capture before stopping. Default `10`
  (≈ 5 request/response pairs). Increase if requests are rare.
- **`capture_timeout`** — optional. Max seconds to wait for packets. Default `60`.

## Rules in force

- **Host-passive:** in-container actions go through `<config: stack.compose_cmd> exec -T` /
  `<config: stack.engine_cmd> exec`; no application code runs on the host.
- **Polling heartbeat:** the host-process poll prints a timestamped heartbeat and has an explicit
  budget (`--poll-sec`).
- **Never a network-wide sniffer:** tcpdump runs only inside the target container with the filter
  `tcp port <internal_port> and tcp[tcpflags] & tcp-push != 0`.
- **No destructive action without the owner:** never kill a process yourself.

## Execution

### Step 1 — Confirm the service is running and traffic is flowing

```bash
<compose_cmd> -p <project_name> ps <service>
<compose_cmd> -p <project_name> logs --since 60s --tail 30 <service>
```

The recent logs should show the suspicious requests. If they don't, either the requests stopped or
this is the wrong service. STOP and report what you see; do not start tcpdump on a quiet container.

### Step 2 — Run the helper

```bash
uv run --no-project .workflow/scripts/trace_port.py \
  --service <service> --internal-port <internal_port> \
  [--project-name <project_name>] [--capture-packets 10] [--capture-timeout 60]
```

- Host-process backend: psutil when importable, else `lsof` + `ps` (Linux/macOS). **Windows
  requires psutil** — run with `uv run --no-project --with psutil ...`. Exit code 8 means no
  backend was available; re-run with psutil.
- If the service runs as a non-root user and tcpdump install/capture fails, re-run with
  `--exec-user 0`.

Surface the entire script output verbatim. The report has three parts:

1. **Request fingerprint** — method, path, User-Agent, Content-Length, first body line. Usually
   names the caller (a curl version → a shell loop; node-fetch → a Node script; python-requests →
   a Python tool).
2. **Source IP classification** — a docker network gateway (HOST-ORIGIN) or a container IP
   (CROSS-CONTAINER).
3. **Source process trace** — host-origin: PID, executable, command line, parent, grandparent.
   Cross-container: source container, PID inside it, command line, parent.

### Step 3 — Interpret + recommend

Report to the caller:

- **What the source is**, in plain language (e.g. "a shell `until` loop spawned by PID X", "a
  scheduled task in the worker container").
- **Why it misfires**, if the fingerprint shows it (empty body, wrong endpoint, missing auth).
- **A concrete fix path** scoped to the actual source. Present PIDs and the kill command as a
  suggestion only — killing processes from another session requires the owner's explicit go-ahead.

## Failure modes (by exit code)

- **4 — no packets within `capture_timeout`:** traffic stopped during the run. Re-run after
  confirming the logs still show it.
- **5 — no source IP parsed:** packets arrived but none yielded a parseable source address. Surface
  the raw capture lines the script printed and ask.
- **3 — tcpdump unavailable:** package repos unreachable, unsupported image, or non-root exec user.
  Surface the install error; suggest `--exec-user 0` or installing tcpdump in the image.
- **Host poll caught nothing:** the source closes its socket faster than the poll cadence. Re-run
  with `--poll-interval-ms 10` or a longer `--poll-sec`.
- **6 — source IP matches neither a gateway nor a container:** the IP belongs to a network the
  script did not enumerate. Surface the IP + `<config: stack.engine_cmd> network ls` and ask.
- **7 — port not published:** host-origin traffic to an unpublished port; check the compose port
  mapping.
- **2 — no running container:** the stack is down or the service name is wrong.

## When NOT to use

- The request origin is already known (use application-side request logging instead).
- Generic traffic capture for later analysis (use tcpdump directly — this agent is for source
  attribution).
- Production troubleshooting — this is a dev-stack tool.
