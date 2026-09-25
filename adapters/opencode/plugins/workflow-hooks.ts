/**
 * workflow-hooks — OpenCode adapter for the agent-workflow-kit hook scripts (DESIGN §7).
 *
 * Every hook's logic lives in Python under .workflow/hooks/ and speaks the Claude Code hook
 * protocol. This plugin builds the Claude-shaped payload for each OpenCode event, spawns the
 * same script, and translates the result:
 *   exit 2 (+ stderr)                               -> throw Error(stderr)   (tool blocked)
 *   stdout {"hookSpecificOutput":{"permissionDecision":"deny",...}} -> throw Error(reason)
 *   anything else, or the script failing to run     -> allow (guards fail open on internal error)
 *
 * Hooks used — all exist in @opencode-ai/plugin `Hooks` (packages/plugin/src/index.ts, dev):
 *   config                                  -> learn MCP server names (tool-name mapping)
 *   chat.message                            -> remember each session's agent (agent_type) and user text
 *   tool.execute.before                     -> PreToolUse guards (throw to block)
 *   tool.execute.after                      -> PostToolUse guard + per-turn tool-call buffer
 *   event: message.part.updated             -> failed tool calls (ToolPart state.status "error")
 *   event: session.created                  -> SessionStart "startup"/"clear" injectors
 *   event: session.compacted                -> SessionStart "compact" injectors
 *   event: session.idle                     -> Stop: session_journal.py with workflow_tool_calls
 *   experimental.session.compacting         -> PreCompact: write_handoff.py + session_retrospective.py
 *   experimental.chat.system.transform      -> context injection (see below)
 *
 * Context injection: OpenCode has no stdout-to-context channel for session start. The supported
 * mechanism used here is the `experimental.chat.system.transform` hook
 * (`(input: { sessionID?: string; model }, output: { system: string[] }) => Promise<void>`,
 * packages/plugin/src/index.ts): the injector output captured at session start / after
 * compaction is appended to `output.system` on every LLM call of that session, so it persists
 * for the whole session the way Claude's SessionStart context does. A session first seen at a
 * chat turn (not created in this process — i.e. resumed) runs the "resume" injector set once.
 *
 * Environment:
 *   WORKFLOW_HOOK_RUNNER   command prefix for scripts (default "uv run --no-project";
 *                          e.g. "python3").
 *   WORKFLOW_OPENCODE_SHELL "bash" (default) or "powershell": which shell guard checks the
 *                          `bash` tool (OpenCode exposes every shell kind as tool id "bash").
 *   WORKFLOW_HEADLESS      set in headless child runs (write_handoff.py); the plugin then skips
 *                          journaling, injection and handoff — guards still apply.
 */
import type { Plugin } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"
import { mkdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"

type HookResult = { code: number; stdout: string; stderr: string }
type ToolCall = { tool: string; input: string; ok: boolean }

const MAX_INPUT = 300
const RUNNER = (process.env.WORKFLOW_HOOK_RUNNER || "uv run --no-project").split(/\s+/).filter(Boolean)
const SHELL_KIND = (process.env.WORKFLOW_OPENCODE_SHELL || "bash").toLowerCase()
const HEADLESS = !!process.env.WORKFLOW_HEADLESS

const BUILTIN_TOOL_NAMES: Record<string, string> = {
  bash: SHELL_KIND === "powershell" ? "PowerShell" : "Bash",
  task: "Agent",
  skill: "Skill",
  write: "Write",
  edit: "Edit",
  read: "Read",
  glob: "Glob",
  grep: "Grep",
  webfetch: "WebFetch",
  websearch: "WebSearch",
}

// PreToolUse / PostToolUse wiring — mirrors adapters/claude/settings.hooks.json.
const PRE_GUARDS: Record<string, string[]> = {
  Agent: ["guards/dispatch_prompt.py", "guards/phase_dispatch.py"],
  Skill: ["guards/phase_dispatch.py"],
  Bash: ["guards/bash_command.py", "guards/agent_source_read.py"],
  PowerShell: ["guards/powershell_command.py", "guards/agent_source_read.py"],
}
const MCP_PRE_GUARDS = ["guards/graph_query.py", "guards/mcp_readonly.py"]
const POST_GUARDS: Record<string, string[]> = {
  Write: ["guards/script_registration.py"],
}

const STARTUP_INJECTORS: [string, string[]][] = [
  ["session/check_pending_proposals.py", []],
  ["session/inject_story_ledger.py", []],
]
const COMPACT_INJECTORS: [string, string[]][] = [
  ["session/inject_handoff.py", []],
  ["session/inject_symbol_nav_directive.py", ["--tool", "opencode"]],
  ["session/inject_story_ledger.py", []],
]
const RESUME_INJECTORS = COMPACT_INJECTORS

function sanitize(v: string): string {
  // Same rule OpenCode uses to build MCP tool keys (packages/opencode/src/mcp/catalog.ts).
  return v.replace(/[^a-zA-Z0-9_-]/g, "_")
}

function summarize(tool: string, args: any): string {
  let s: string
  if (args && typeof args === "object") {
    s = args.command ?? args.filePath ?? args.file_path ?? args.subagent_type ?? args.name ?? JSON.stringify(args)
    if (tool === "Agent" && args.description) s = `${args.subagent_type ?? ""}: ${args.description}`
  } else {
    s = String(args ?? "")
  }
  s = String(s)
  return s.length > MAX_INPUT ? s.slice(0, MAX_INPUT) : s
}

export const WorkflowHooks: Plugin = async ({ client, directory, worktree }) => {
  const root = worktree || directory
  const hooksDir = join(root, ".workflow", "hooks")
  const mcpServers: string[] = []
  const sessionAgent = new Map<string, string>()
  const sessionParent = new Map<string, string | null>()
  const createdHere = new Set<string>()
  const injected = new Map<string, string>()
  const injecting = new Map<string, Promise<void>>()
  const turnCalls = new Map<string, ToolCall[]>()
  const turnUserText = new Map<string, string[]>()
  const countedCalls = new Set<string>()

  function runHook(rel: string, payload: unknown, args: string[] = [], timeoutMs = 60_000): Promise<HookResult> {
    return new Promise((resolve) => {
      let stdout = ""
      let stderr = ""
      let done = false
      const finish = (r: HookResult) => {
        if (!done) {
          done = true
          resolve(r)
        }
      }
      try {
        const [cmd, ...pre] = RUNNER
        const child = spawn(cmd, [...pre, join(hooksDir, rel), ...args], {
          cwd: root,
          env: process.env,
          stdio: ["pipe", "pipe", "pipe"],
        })
        const timer = setTimeout(() => {
          child.kill()
          finish({ code: 0, stdout: "", stderr: "" }) // fail open on a hung hook
        }, timeoutMs)
        child.stdout.on("data", (d) => (stdout += d.toString()))
        child.stderr.on("data", (d) => (stderr += d.toString()))
        child.on("error", () => {
          clearTimeout(timer)
          finish({ code: 0, stdout: "", stderr: "" })
        })
        child.on("close", (code) => {
          clearTimeout(timer)
          finish({ code: code ?? 0, stdout, stderr })
        })
        child.stdin.end(JSON.stringify(payload))
      } catch {
        finish({ code: 0, stdout: "", stderr: "" })
      }
    })
  }

  function denyReason(r: HookResult): string | null {
    if (r.code === 2) return r.stderr.trim() || "blocked by workflow hook"
    const out = r.stdout.trim()
    if (!out.startsWith("{")) return null
    try {
      const j = JSON.parse(out)
      const h = j?.hookSpecificOutput
      if (h?.permissionDecision === "deny") return String(h.permissionDecisionReason || "blocked by workflow hook")
    } catch {
      /* not a decision object */
    }
    return null
  }

  function claudeToolName(tool: string): string {
    if (BUILTIN_TOOL_NAMES[tool]) return BUILTIN_TOOL_NAMES[tool]
    // Longest server name first so "a_b" wins over "a" for key "a_b_tool".
    for (const server of [...mcpServers].sort((a, b) => b.length - a.length)) {
      const prefix = sanitize(server) + "_"
      if (tool.startsWith(prefix)) return `mcp__${server}__${tool.slice(prefix.length)}`
    }
    return tool
  }

  function claudeToolInput(name: string, args: any): Record<string, unknown> {
    const a = args ?? {}
    switch (name) {
      case "Bash":
      case "PowerShell":
        return { command: a.command ?? "", description: a.description }
      case "Agent":
        return { prompt: a.prompt ?? "", subagent_type: a.subagent_type ?? "", description: a.description }
      case "Skill":
        return { skill: a.name ?? "", args: a.args ?? "" }
      case "Write":
        return { file_path: a.filePath ?? "", content: a.content }
      case "Edit":
        return { file_path: a.filePath ?? "", old_string: a.oldString, new_string: a.newString }
      case "Read":
        return { file_path: a.filePath ?? "" }
      default:
        return typeof a === "object" ? a : { value: a }
    }
  }

  async function parentOf(sessionID: string): Promise<string | null> {
    if (sessionParent.has(sessionID)) return sessionParent.get(sessionID) ?? null
    let parent: string | null = null
    try {
      const res: any = await client.session.get({ path: { id: sessionID } })
      parent = res?.data?.parentID ?? null
    } catch {
      parent = null
    }
    sessionParent.set(sessionID, parent)
    return parent
  }

  async function basePayload(event: string, sessionID: string): Promise<Record<string, unknown>> {
    const p: Record<string, unknown> = { hook_event_name: event, session_id: sessionID, cwd: root }
    // agent_type names the CALLER and is present only inside a subagent (Claude semantics).
    if (await parentOf(sessionID)) {
      const agent = sessionAgent.get(sessionID)
      if (agent) p.agent_type = agent
    }
    return p
  }

  function runInjectors(sessionID: string, source: string, set: [string, string[]][]): Promise<void> {
    if (HEADLESS) return Promise.resolve()
    const p = collectInjections(sessionID, source, set)
    injecting.set(sessionID, p)
    return p
  }

  async function collectInjections(sessionID: string, source: string, set: [string, string[]][]) {
    const chunks: string[] = []
    for (const [rel, args] of set) {
      const r = await runHook(rel, { hook_event_name: "SessionStart", source, session_id: sessionID, cwd: root }, args)
      if (r.code === 0 && r.stdout.trim()) chunks.push(r.stdout.trim())
    }
    injected.set(sessionID, chunks.join("\n\n"))
  }

  function pushCall(sessionID: string, call: ToolCall) {
    const list = turnCalls.get(sessionID) ?? []
    list.push(call)
    turnCalls.set(sessionID, list)
  }

  async function exportRecentMessages(sessionID: string): Promise<string | undefined> {
    try {
      const res: any = await client.session.messages({ path: { id: sessionID } })
      const msgs: any[] = (res?.data ?? []).slice(-60)
      const lines: string[] = []
      for (const m of msgs) {
        const role = m?.info?.role ?? "?"
        for (const part of m?.parts ?? []) {
          if (part?.type === "text" && part.text) lines.push(`[${role}] ${String(part.text).slice(0, 2000)}`)
          else if (part?.type === "tool")
            lines.push(`[tool ${part.tool} ${part.state?.status ?? ""}] ${summarize(part.tool, part.state?.input)}`)
        }
      }
      const dir = join(root, ".workflow", "state", "compaction")
      mkdirSync(dir, { recursive: true })
      const file = join(dir, `${sessionID}.txt`)
      writeFileSync(file, lines.join("\n"), "utf8")
      return file
    } catch {
      return undefined
    }
  }

  return {
    config: async (cfg: any) => {
      for (const name of Object.keys(cfg?.mcp ?? {})) if (!mcpServers.includes(name)) mcpServers.push(name)
    },

    "chat.message": async (input, output) => {
      if (input.agent) sessionAgent.set(input.sessionID, input.agent)
      const text = (output.parts ?? [])
        .filter((p: any) => p?.type === "text" && !p?.synthetic)
        .map((p: any) => String(p.text ?? ""))
        .join("\n")
        .trim()
      if (text) {
        const list = turnUserText.get(input.sessionID) ?? []
        list.push(text.slice(0, 500))
        turnUserText.set(input.sessionID, list)
      }
    },

    "tool.execute.before": async (input, output) => {
      const name = claudeToolName(input.tool)
      const scripts = name.startsWith("mcp__") ? MCP_PRE_GUARDS : PRE_GUARDS[name]
      if (!scripts) return
      const payload = {
        ...(await basePayload("PreToolUse", input.sessionID)),
        tool_name: name,
        tool_input: claudeToolInput(name, output.args),
      }
      for (const rel of scripts) {
        const reason = denyReason(await runHook(rel, payload))
        if (reason) throw new Error(reason)
      }
    },

    "tool.execute.after": async (input, output) => {
      const name = claudeToolName(input.tool)
      countedCalls.add(input.callID)
      pushCall(input.sessionID, { tool: name, input: summarize(name, input.args), ok: true })
      const scripts = POST_GUARDS[name]
      if (!scripts) return
      const payload = {
        ...(await basePayload("PostToolUse", input.sessionID)),
        tool_name: name,
        tool_input: claudeToolInput(name, input.args),
      }
      for (const rel of scripts) {
        const r = await runHook(rel, payload)
        // PostToolUse feedback (stderr, exit 0) reaches the agent by appending to the tool output.
        const note = r.stderr.trim()
        if (note) output.output = `${output.output ?? ""}\n\n${note}`
      }
    },

    event: async ({ event }) => {
      const e: any = event
      switch (e.type) {
        case "session.created": {
          const info = e.properties?.info
          if (!info?.id) return
          sessionParent.set(info.id, info.parentID ?? null)
          createdHere.add(info.id)
          if (!info.parentID) await runInjectors(info.id, "startup", STARTUP_INJECTORS)
          return
        }
        case "session.compacted": {
          const id = e.properties?.sessionID
          if (id && !(await parentOf(id))) await runInjectors(id, "compact", COMPACT_INJECTORS)
          return
        }
        case "message.part.updated": {
          const part = e.properties?.part
          if (part?.type === "tool" && part.state?.status === "error" && !countedCalls.has(part.callID)) {
            countedCalls.add(part.callID)
            const name = claudeToolName(part.tool)
            pushCall(part.sessionID, { tool: name, input: summarize(name, part.state?.input), ok: false })
          }
          return
        }
        case "session.idle": {
          const id = e.properties?.sessionID
          if (!id || HEADLESS) return
          const calls = turnCalls.get(id) ?? []
          const userText = turnUserText.get(id) ?? []
          turnCalls.delete(id)
          turnUserText.delete(id)
          const payload = {
            ...(await basePayload("Stop", id)),
            workflow_tool_calls: calls,
            workflow_user_messages: userText,
          }
          await runHook("session/session_journal.py", payload)
          return
        }
      }
    },

    "experimental.session.compacting": async (input, _output) => {
      if (HEADLESS) return
      const transcript = await exportRecentMessages(input.sessionID)
      const payload = {
        hook_event_name: "PreCompact",
        trigger: "auto",
        session_id: input.sessionID,
        cwd: root,
        transcript_path: transcript,
      }
      await runHook("session/write_handoff.py", payload, ["--tool", "opencode"], 240_000)
      await runHook("session/session_retrospective.py", payload)
    },

    "experimental.chat.system.transform": async (input, output) => {
      const id = input.sessionID
      if (!id || HEADLESS) return
      const pending = injecting.get(id)
      if (pending) await pending
      if (!injected.has(id) && !createdHere.has(id) && !(await parentOf(id))) {
        await runInjectors(id, "resume", RESUME_INJECTORS)
      }
      const text = injected.get(id)
      if (text) output.system.push(text)
    },
  }
}

