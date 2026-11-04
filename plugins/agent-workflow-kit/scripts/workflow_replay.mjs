#!/usr/bin/env node
// Replay runner for a Claude Code Workflow script where no Workflow runtime exists (OpenCode, or a Claude Code
// session without the Workflow tool). The orchestrator plays the runtime's part: it dispatches each agent call the
// script makes and records the result; this runner re-executes the deterministic script, answering every agent()
// call it has a recorded result for, until the script completes.
//
//   node <plugin-root>/scripts/workflow_replay.mjs run <script.js> <out_dir>
//     args come from <out_dir>/args.json. Exit 0 + `COMPLETE <out_dir>/run.json` when the script returned;
//     exit 10 + `PENDING <n> <out_dir>/pending.json` when it waits on agent calls with no recorded result;
//     exit 1 + `FAILED <message>` when the script threw (run.json then has status `failed`).
//   node <plugin-root>/scripts/workflow_replay.mjs record <out_dir> <label> <result.json>
//   node <plugin-root>/scripts/workflow_replay.mjs record <out_dir> <label> --error "<message>"
//     records one agent call's result (a JSON file holding the agent's structured report, `null` for no report)
//     or its failure. Refuses (exit 2) a label that is not pending or already answered, and a result that lacks a
//     required top-level key of the call's schema (record it with --error instead, which the script treats as a
//     failed agent and retries where it retries).
//
// pending.json lists each waiting call: {label, agentType, effort, phase, schema, prompt, dispatch_prompt}. Dispatch
// every listed call (the ones listed together may run in parallel) as the typed subagent `agentType` with
// `dispatch_prompt` verbatim, record each result, then `run` again. Files: <out_dir>/journal.jsonl ({type: started,
// label, agentId, ...} and {type: result, agentId, result|error} lines), <out_dir>/run.json ({runId, workflowName,
// status, result, logs}) — the layout review_findings_render.py reads with --run-dir.
//
// The script is evaluated with the runtime globals args, agent, parallel, pipeline, phase and log; `export const
// meta` is read for the workflow name. Requires Node >= 18.
import { readFileSync, writeFileSync, appendFileSync, existsSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

const [cmd, ...rest] = process.argv.slice(2)

function readJournal(out) {
  const path = join(out, 'journal.jsonl')
  if (!existsSync(path)) return { started: new Map(), results: new Map() }
  const started = new Map()
  const results = new Map()
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    if (!line.trim()) continue
    const row = JSON.parse(line)
    if (row.type === 'started') started.set(row.label, row)
    else if (row.type === 'result') results.set(row.agentId, row)
  }
  return { started, results }
}

function schemaInstruction(schema) {
  return schema
    ? '\n\nReturn your report as one JSON object, with no prose around it, that matches this JSON schema:\n' +
      JSON.stringify(schema)
    : ''
}

async function run(script, out) {
  const args = JSON.parse(readFileSync(join(out, 'args.json'), 'utf8'))
  const { started, results } = readJournal(out)
  const idFile = join(out, 'run-id')
  if (!existsSync(idFile)) writeFileSync(idFile, randomUUID())
  const runId = readFileSync(idFile, 'utf8').trim()
  const pending = []
  const logs = []
  const meta = {}

  function agent(prompt, opts = {}) {
    const label = String(opts.label || '')
    if (!label) return Promise.reject(new Error('workflow_replay: every agent() call needs a unique label'))
    const s = started.get(label)
    const r = s && results.get(s.agentId)
    if (r) return 'error' in r ? Promise.reject(new Error(String(r.error))) : Promise.resolve(r.result ?? null)
    pending.push({ label, agentType: opts.agentType || '', effort: opts.effort || '', phase: opts.phase || '',
      schema: opts.schema || null, prompt, dispatch_prompt: prompt + schemaInstruction(opts.schema) })
    return new Promise(() => {})
  }
  const parallel = thunks => Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)))
  const pipeline = (items, ...stages) => parallel(items.map(item => () =>
    stages.reduce((p, stage) => p.then(v => (v == null ? v : stage(v))), Promise.resolve(item))))
  const body = readFileSync(script, 'utf8').replace(/^export const meta =/m, 'meta.value =')
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
  const fn = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'meta', body)
  let settled = false
  const writeRun = (status, result, error) => writeFileSync(join(out, 'run.json'), JSON.stringify({
    runId, workflowName: meta.value && meta.value.name, status, result, ...(error ? { error } : {}), logs }))
  process.on('beforeExit', () => {
    if (settled) return
    settled = true
    writeFileSync(join(out, 'pending.json'), JSON.stringify(pending, null, 1))
    for (const p of pending) {
      if (!started.has(p.label)) {
        const agentId = `replay-${randomUUID()}`
        started.set(p.label, { agentId })
        appendFileSync(join(out, 'journal.jsonl'), JSON.stringify({ type: 'started', label: p.label, agentId,
          agentType: p.agentType, phase: p.phase, prompt: p.prompt, schema: p.schema }) + '\n')
      }
    }
    process.stdout.write(`PENDING ${pending.length} ${join(out, 'pending.json')}\n`)
    process.exit(10)
  })
  try {
    const result = await fn(args, agent, parallel, pipeline, () => {}, m => logs.push(String(m)), meta)
    settled = true
    writeRun('completed', result)
    writeFileSync(join(out, 'pending.json'), '[]')
    process.stdout.write(`COMPLETE ${join(out, 'run.json')}\n`)
    process.exit(0)
  } catch (e) {
    settled = true
    writeRun('failed', null, String((e && e.message) || e))
    process.stdout.write(`FAILED ${String((e && e.message) || e)}\n`)
    process.exit(1)
  }
}

function record(out, label, rest) {
  const { started, results } = readJournal(out)
  const s = started.get(label)
  if (!s) { process.stderr.write(`record: ${label} is not a pending call (run the script first)\n`); process.exit(2) }
  if (results.has(s.agentId)) { process.stderr.write(`record: ${label} already has a result\n`); process.exit(2) }
  let row
  if (rest[0] === '--error') {
    row = { type: 'result', agentId: s.agentId, error: rest.slice(1).join(' ') || 'agent error' }
  } else {
    const result = JSON.parse(readFileSync(rest[0], 'utf8'))
    const required = s.schema && Array.isArray(s.schema.required) ? s.schema.required : []
    const absent = result && typeof result === 'object' ? required.filter(k => !(k in result)) : []
    if (absent.length) {
      process.stderr.write(`record: ${label} result lacks required ${absent.join(', ')}; ` +
        're-dispatch the agent, or record --error "<why>"\n')
      process.exit(2)
    }
    row = { type: 'result', agentId: s.agentId, result }
  }
  appendFileSync(join(out, 'journal.jsonl'), JSON.stringify(row) + '\n')
  process.stdout.write(`RECORDED ${label}\n`)
}

if (cmd === 'run' && rest.length === 2) {
  mkdirSync(rest[1], { recursive: true })
  await run(rest[0], rest[1])
} else if (cmd === 'record' && rest.length >= 3) {
  record(rest[0], rest[1], rest.slice(2))
} else {
  process.stderr.write('usage: workflow_replay.mjs run <script.js> <out_dir> | record <out_dir> <label> <result.json>|--error <msg>\n')
  process.exit(2)
}
