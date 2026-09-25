---
name: trace-port-traffic
description: Identify what is generating mystery HTTP traffic to a compose service port — captures the request body and User-Agent inside the target container with tcpdump, then traces the source process. Handles both host-origin traffic (host process lookup via psutil or lsof) and cross-container traffic (docker network IP → container → /proc-based PID lookup). Use when service logs show repeated unexpected requests and you need the spawning process and command line.
---

Requires a container stack (`config: stack.runtime` ≠ `none`); otherwise say so and stop.

Dispatch the `trace-port-traffic` subagent. Pass in the prompt:
- `service` — required: compose service name where the requests land
- `internal_port` — required: port the service listens on inside the container
- `project_name` — optional (default: `config: slots.shared_stack_name`)
- `capture_packets` — optional (default: `10`)
- `capture_timeout` — optional (default: `60`)

The subagent runs `.workflow/scripts/trace_port.py`. Report its findings verbatim.
