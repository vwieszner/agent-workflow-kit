export const meta = {
  name: 'review',
  description: 'Story code review: typed review layers per diff chunk, execution stages (UI check, property tests, mutation testing) on the story stack, code dedupe, batched findings verification, majority votes in auto mode, repro tests; returns the record map the findings renderer resolves against the run journal',
  whenToUse: 'Invoked by the layered-review skill (pipeline mode) only; args are staged by .workflow/scripts/review_stage.py.',
  phases: [
    { title: 'Review', detail: 'blind-hunter, edge-case-hunter, verification-gap, acceptance-auditor per diff chunk' },
    { title: 'Execute', detail: 'ui-verifier, pbt-writer, test-adequacy on the story stack, one at a time, alongside the review layers' },
    { title: 'Verify', detail: 'findings-verifier per batch of deduped findings' },
    { title: 'Vote', detail: 'auto mode: two more findings-verifier votes on patch and bar-blocking candidates' },
    { title: 'Repro', detail: 'repro-writer, one batch at a time, on non-refuted runtime findings' },
  ],
}

// args (all paths absolute): story_id, round, mode ('controlled'|'auto'), passes, date,
// diff_range, worktree, run_dir, diff_chunks ([{path, lines, pages}] from review_diff_chunks.py),
// claims_file ('' = none), spec_file ('' = no-spec), graph_project ('' = none),
// snapshot_dir ('' = live worktree), prompts_dir, repo_root, findings_file,
// stack_project_name ('' = no repro or execution stages), changed_files (repo-relative paths of the
// diff; [] = execution stages skipped), branch_files (repo-relative paths of the branch's whole change,
// merge-base through the end of diff_range; the ui-verifier is dispatched with the branch's own UI profile
// when it lists that file; [] or absent = the preloaded profile), ui_base_url ('' = no ui-verifier),
// ui_chunks ([{path, lines, pages, files, test_titles, test_viewports}] of the UI-only diff (config review.ui_paths),
// over diff_range or, when no earlier round's ui-verifier completed or branch_files list the UI profile, the whole
// story; the ui-verifier reads only these and runs when ui_sources is non-empty;
// test_titles and test_viewports list the titles of the tests and the viewports the chunk's test files add or set,
// as review_diff_chunks.py prints them), spec_acs (the AC ids the spec declares, as review_spec_acs.py prints them;
// [] = none; required whenever the ui-verifier stage runs),
// carried ([{id, round, title, location, kind, recommended, claim, verdict, bar_impact}] from
// review_carry.py: the triage findings of the halted review rounds this round carries; [] or
// absent = none; any other value is refused),
// delta ({kind: 'branch'|'close-out'|'new-code', reviewed: {base, end} (40-hex), against, how, replay, reason, lines,
// hunks: {path: [[start, end], ...]}, deleted: [path]} from review_stage.py; required, returned unchanged),
// preamble (config review.required_preamble, required), quality_bar_doc ('' = no bar), graph_main_project,
// path_rewrites ([[regex, replacement]]), worktrees_dir, ui_profile ('' = none), ui_profile_from_branch,
// ui_sources / backend_sources / backend_tests / pbt_sources (repo-relative changed files each stage reads; [] = stage skipped).
const A = args
const HEX40 = /^[0-9a-f]{40}$/
if (!A.delta || typeof A.delta !== 'object' || Array.isArray(A.delta))
  throw new Error('review: args.delta must be an object')
if (!['branch', 'close-out', 'new-code'].includes(A.delta.kind))
  throw new Error('review: args.delta.kind must be branch, close-out or new-code')
if (!A.delta.reviewed || typeof A.delta.reviewed !== 'object')
  throw new Error('review: args.delta.reviewed must be an object with base and end')
for (const k of ['base', 'end'])
  if (typeof A.delta.reviewed[k] !== 'string' || !HEX40.test(A.delta.reviewed[k]))
    throw new Error(`review: args.delta.reviewed.${k} must be a 40-hex sha`)
const hunksOk = A.delta.hunks && typeof A.delta.hunks === 'object' && !Array.isArray(A.delta.hunks) &&
  Object.values(A.delta.hunks).every(rs => Array.isArray(rs) &&
    rs.every(r => Array.isArray(r) && r.length === 2 && r.every(Number.isInteger)))
if (!hunksOk) throw new Error('review: args.delta.hunks must map paths to [int, int] ranges')
if (!Array.isArray(A.delta.deleted) || !A.delta.deleted.every(p => typeof p === 'string'))
  throw new Error('review: args.delta.deleted must be a list of paths')
const PASSES = Math.max(1, Number(A.passes || 1))
const CHUNKS = A.diff_chunks || []
const UI_CHUNKS = A.ui_chunks || []
// A chunk with no title or viewport list is refused: read as an empty list it would report every title mapped
// and every viewport placed.
UI_CHUNKS.forEach((c, i) => {
  if (!c || !Array.isArray(c.test_titles) || !c.test_titles.every(t => typeof t === 'string' && t))
    throw new Error(`review: args.ui_chunks[${i}] has no test_titles list of titles`)
  if (!Array.isArray(c.test_viewports) || !c.test_viewports.every(v => typeof v === 'string' && v))
    throw new Error(`review: args.ui_chunks[${i}] has no test_viewports list of viewports`)
})
const UI_TITLES = [...new Set(UI_CHUNKS.flatMap(c => c.test_titles))]
const UI_VIEWPORTS = [...new Set(UI_CHUNKS.flatMap(c => c.test_viewports))]
// A spec_acs value that is not a list of ids is refused; a missing one is refused below, once the ui-verifier stage is known to
// run, because read as an empty list it would report every AC mapped.
const ACS_OK = Array.isArray(A.spec_acs) && A.spec_acs.every(a => typeof a === 'string' && a)
if (A.spec_acs !== undefined && !ACS_OK) throw new Error('review: args.spec_acs is not a list of AC ids')
const SPEC_ACS = ACS_OK ? [...new Set(A.spec_acs)] : []
if (A.carried !== undefined && A.carried !== null && !Array.isArray(A.carried))
  throw new Error('review: args.carried must be a JSON array')
const CARRIED = A.carried || []
const CARRIED_KEYS = ['id', 'title', 'location', 'kind', 'recommended', 'claim', 'verdict', 'bar_impact']
CARRIED.forEach((c, i) => {
  const bad = !c || typeof c !== 'object' || !Number.isInteger(c.round) || c.round >= Number(A.round) ||
    CARRIED_KEYS.some(k => typeof c[k] !== 'string')
  if (bad) throw new Error(`review: args.carried[${i}] is malformed or not from an earlier round`)
  if (CARRIED.findIndex(x => x.id === c.id) !== i) throw new Error(`review: args.carried repeats ${c.id}`)
})
if (typeof A.preamble !== 'string' || !A.preamble.trim()) throw new Error('review: args.preamble (config review.required_preamble) is required')
// DEFER_CLAUSE is a verbatim substring of skills/rules-defer/SKILL.md (pinned by scripts/tests/test_review_workflow.py).
const DEFER_CLAUSE = 'defer only when the fix needs scope outside this story or a design or spec decision'
const TRIAGE_NOTE = A.mode === 'auto' ? ' In this run the findings-evaluator agent performs triage.' : ''
const CLASSIFY = `Classify under the rules-defer rule: patch by default; ${DEFER_CLAUSE}.`
const PREAMBLE = `${A.preamble.trim()}${TRIAGE_NOTE} ${CLASSIFY}`
// Effort per agent type. Workflow spawns do not show whether frontmatter `effort` applies,
// so every agent() call passes it; scripts/check_agent_frontmatter.py keeps this map equal
// to each agent's frontmatter.
const EFFORT = {
  'blind-hunter': 'high',
  'edge-case-hunter': 'high',
  'verification-gap': 'high',
  'acceptance-auditor': 'high',
  'findings-verifier': 'high',
  'repro-writer': 'high',
  'ui-verifier': 'high',
  'pbt-writer': 'high',
  'test-adequacy': 'high',
}

const CODE_ROOT = A.snapshot_dir || A.worktree
const CODE_ACCESS = (A.snapshot_dir
  ? `SNAPSHOT MODE: the worktree is being mutated by another agent. Read code only under ${A.snapshot_dir} (files materialized at the reviewed commit) and do not use the graph.`
  : `Code under review lives in the worktree ${A.worktree}. ` +
    (A.graph_project
      ? `Every code-graph call passes project=${A.graph_project}${A.graph_main_project ? ` (never ${A.graph_main_project}, the main checkout's graph)` : ''}.`
      : 'No graph project: use Grep/Read only.')) +
  ` Every Grep/Glob passes an absolute path under ${CODE_ROOT}; your working directory is the main checkout, which holds a different branch.`

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['findings', 'checked', 'diff_lines_read'],
  properties: {
    checked: { type: 'string', description: 'What was reviewed; required even when findings is empty.' },
    diff_lines_read: { type: 'integer', description: 'How many lines of the diff chunk you actually read.' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'location', 'claim', 'evidence', 'kind', 'recommended'],
        properties: {
          title: { type: 'string', maxLength: 200 },
          location: { type: 'string', description: 'file:line, file:start-end, spec AC id, or N/A', maxLength: 300 },
          claim: { type: 'string', maxLength: 2000 },
          evidence: { type: 'string', maxLength: 4000 },
          kind: { type: 'string', enum: ['general', 'edge', 'deletion', 'claim', 'gap', 'ac', 'ui'] },
          recommended: { type: 'string', enum: ['patch', 'defer', 'dismiss'], description: `Your classification under the rules-defer rule: patch by default; ${DEFER_CLAUSE}, naming that scope or decision in claim.` },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
      },
    },
  },
}

// How a state came to be `unreached`: one of the four ways of reaching a state, or `rule` for a state a rule of the agent
// text places there without a method tried. `UNREACHED_METHODS` in review_findings_render.py repeats the list.
const UNREACHED_METHODS = ['seeded', 'created', 'shared_data', 'websocket_or_fault', 'rule']

// The ui-verifier also lists every screen and state it did not check on screen (`unreached`)
// and, apart, every state it places outside the story (`out_of_scope`), so a report with no
// findings never reads as full coverage; the renderer prints both lists at triage. It returns
// its own state list (`states`) and places each id once: in `checked_ids`, or as the `state_id`
// of an `unreached` or `out_of_scope` entry; `uiAccounting` names the ids placed nowhere, more
// than once, or absent from `states`. It also maps each test title the dispatch lists to the state
// that test drives (`tests`), each viewport the dispatch lists to the layout states at that viewport
// (`viewports`) and each spec AC id the dispatch lists to the states that AC describes (`acs`):
// `uiAccounting` names a dispatched title, viewport or AC with no entry, and an entry's `state_id`
// absent from `states` joins the absent ids. An entry is a mapping, never a placement. An `unreached` entry also names
// the methods it tried (`methods_tried`); `uiAccounting` refuses an entry whose list is missing, empty or malformed.
const UI_SCHEMA = {
  ...FINDINGS_SCHEMA,
  required: [...FINDINGS_SCHEMA.required, 'states', 'checked_ids', 'tests', 'viewports', 'acs', 'unreached', 'out_of_scope'],
  properties: {
    ...FINDINGS_SCHEMA.properties,
    states: {
      type: 'array',
      description: 'Every screen and state your report places, one entry each, ids S1, S2, … each used once; [] when none.',
      items: {
        type: 'object',
        required: ['id', 'state'],
        properties: {
          id: { type: 'string', minLength: 1, maxLength: 20 },
          state: { type: 'string', minLength: 1, maxLength: 300 },
        },
      },
    },
    checked_ids: {
      type: 'array',
      description: 'The id of every state in `states` that you checked on screen; [] when none.',
      items: { type: 'string', minLength: 1, maxLength: 20 },
    },
    tests: {
      type: 'array',
      description: 'One entry for every test title the dispatch lists, the title copied exactly, with the id in `states` of the state that test drives; repeat a title to map it to a further state; [] when the dispatch lists none.',
      items: {
        type: 'object',
        required: ['title', 'state_id'],
        properties: {
          title: { type: 'string', minLength: 1, maxLength: 1000 },
          state_id: { type: 'string', minLength: 1, maxLength: 20, description: 'The id of the state this test drives, in `states`.' },
        },
      },
    },
    viewports: {
      type: 'array',
      description: 'One entry for every viewport the dispatch lists, the viewport copied exactly, with `state_ids` the ids in `states` of the layout states you check or place at that viewport; [] when the dispatch lists none.',
      items: {
        type: 'object',
        required: ['viewport', 'state_ids'],
        properties: {
          viewport: { type: 'string', minLength: 1, maxLength: 40 },
          state_ids: {
            type: 'array',
            minItems: 1,
            description: 'The ids in `states` of the layout states at this viewport.',
            items: { type: 'string', minLength: 1, maxLength: 20 },
          },
        },
      },
    },
    acs: {
      type: 'array',
      description: 'One entry for every AC id the dispatch lists, the id copied exactly, with `state_ids` the ids in `states` of the states that AC describes, or `state_ids` as [] and a `reason` when it describes no screen state; [] when the dispatch lists none.',
      items: {
        type: 'object',
        required: ['ac', 'state_ids'],
        properties: {
          ac: { type: 'string', minLength: 1, maxLength: 40 },
          state_ids: {
            type: 'array',
            description: 'The ids in `states` of the states this AC describes; [] when it describes none.',
            items: { type: 'string', minLength: 1, maxLength: 20 },
          },
          reason: { type: 'string', maxLength: 500, description: 'Why the AC describes no state; required when state_ids is empty.' },
        },
      },
    },
    unreached: {
      type: 'array',
      description: 'Every screen or state from your list that you did not check on screen; [] when none.',
      items: {
        type: 'object',
        required: ['state_id', 'state', 'reason', 'methods_tried'],
        properties: {
          state_id: { type: 'string', minLength: 1, maxLength: 20, description: 'The id of this state in `states`.' },
          state: { type: 'string', maxLength: 300 },
          reason: { type: 'string', maxLength: 500 },
          methods_tried: {
            type: 'array',
            minItems: 1,
            description: 'The methods you tried to reach this state, one item per method, in the order tried. A state a rule of the agent text places here without a method tried has one item, with method `rule`.',
            items: {
              type: 'object',
              required: ['method', 'stopped_by'],
              properties: {
                method: { type: 'string', enum: UNREACHED_METHODS },
                stopped_by: { type: 'string', minLength: 1, maxLength: 500, description: 'What stopped this method; for `rule`, the rule.' },
              },
            },
          },
        },
      },
    },
    out_of_scope: {
      type: 'array',
      description: 'Every state you place outside this story, with the kind of reason; [] when none.',
      items: {
        type: 'object',
        required: ['state_id', 'state', 'kind', 'reason'],
        properties: {
          state_id: { type: 'string', minLength: 1, maxLength: 20, description: 'The id of this state in `states`.' },
          state: { type: 'string', minLength: 1, maxLength: 300 },
          kind: { type: 'string', enum: ['not_in_spec_or_diff', 'destructive_to_seed'] },
          reason: { type: 'string', minLength: 1, maxLength: 500 },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['verdicts'],
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'verdict', 'evidence', 'bar_impact'],
        properties: {
          id: { type: 'string' },
          verdict: { type: 'string', enum: ['CONFIRMED', 'REFUTED', 'UNVERIFIABLE'] },
          evidence: { type: 'string', maxLength: 4000 },
          bar_impact: { type: 'string', description: "'none — <why>', 'BLOCKING — <bar-id> — <consequence>', 'n/a (no quality bar configured)', or 'n/a' for REFUTED", maxLength: 500 },
        },
      },
    },
  },
}

const REPRO_SCHEMA = {
  type: 'object',
  required: ['results'],
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'status', 'failure_kind', 'test_path', 'test_id', 'tail'],
        properties: {
          id: { type: 'string' },
          status: { type: 'string', enum: ['REPRODUCED', 'NOT-REPRODUCED', 'BLOCKED', 'UNTESTABLE'] },
          failure_kind: { type: 'string', enum: ['assertion', 'error', 'collection', 'none'],
            description: "'assertion': an assertion in the test body failed; 'error': an exception outside an assertion; 'collection': the test never ran (import error, wrong label, file not in the container); 'none': it passed or was not run" },
          test_path: { type: 'string', description: "worktree-relative path of the repro test file, or ''", maxLength: 300 },
          test_id: { type: 'string', description: "the runner's test id, or ''", maxLength: 300 },
          tail: { type: 'string', description: 'the failure tail, or why BLOCKED / UNTESTABLE', maxLength: 1500 },
        },
      },
    },
  },
}

// pbt-writer: each finding is a falsified property and carries the test that falsified it.
const PBT_SCHEMA = {
  type: 'object',
  required: ['findings', 'checked', 'diff_lines_read', 'properties'],
  properties: {
    checked: { type: 'string', description: 'Which functions were targeted, and why others were not.' },
    diff_lines_read: { type: 'integer', description: 'How many diff lines you actually read, over all diff files.' },
    properties: {
      type: 'array',
      items: {
        type: 'object',
        required: ['function', 'property', 'status', 'test_path'],
        properties: {
          function: { type: 'string', maxLength: 300 },
          property: { type: 'string', maxLength: 500 },
          status: { type: 'string', enum: ['FALSIFIED', 'HELD', 'BLOCKED'] },
          test_path: { type: 'string', maxLength: 300 },
        },
      },
    },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'location', 'claim', 'evidence', 'kind', 'recommended', 'failure_kind', 'test_path', 'test_id', 'tail'],
        properties: {
          title: { type: 'string', maxLength: 200 },
          location: { type: 'string', description: 'file:line of the function under test', maxLength: 300 },
          claim: { type: 'string', maxLength: 2000 },
          evidence: { type: 'string', description: 'the minimal falsifying example and the property it breaks', maxLength: 4000 },
          kind: { type: 'string', enum: ['property'] },
          recommended: { type: 'string', enum: ['patch', 'defer', 'dismiss'], description: `Your classification under the rules-defer rule: patch by default; ${DEFER_CLAUSE}, naming that scope or decision in claim.` },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          failure_kind: { type: 'string', enum: ['assertion', 'error', 'collection'] },
          test_path: { type: 'string', description: 'worktree-relative path of the property test file', maxLength: 300 },
          test_id: { type: 'string', maxLength: 300 },
          tail: { type: 'string', maxLength: 1500 },
        },
      },
    },
  },
}

const passNote = pass => (PASSES > 1 ? `Independent review pass ${pass} of ${PASSES}.\n` : '')
// Pages come from review_diff_chunks.py, sized to the Read tool's token limit; a line-count page
// size cannot be, because line lengths differ tenfold between diffs.
const pageCalls = c => {
  if (!Array.isArray(c.pages) || !c.pages.length) throw new Error(`review: diff chunk ${c.path} has no pages`)
  return c.pages.map(([o, l]) => `${o}/${l}`).join(', ')
}
const READ_RULE = 'Make exactly these Read calls; a larger page exceeds the Read tool\'s token limit. ' +
  'A report submitted before every line is read is refused.'
const chunkNote = (c, k) =>
  `Diff file: ${c.path} (chunk ${k + 1} of ${CHUNKS.length}, ${c.lines} lines). Read ALL of it with Read calls ` +
  `(offset/limit) ${pageCalls(c)}. ${READ_RULE} Set diff_lines_read to the number of lines you read. ` +
  `Each hunk line starts with its line number in the source file (o<N> = line N of the old file, for a removed line); ` +
  `cite locations as file:<that number>, never the line number the Read tool shows for the diff file. ` +
  `A line ending in "[review_diff_chunks: line clipped at <C> of <N> chars]" is cut in this file: report each such line ` +
  `as a finding of kind general titled "<N>-char line clipped, not reviewable", at its file:<number>.`

const LAYERS = [
  {
    id: 'blind-hunter',
    active: true,
    prompt: (pass, c, k) => `${PREAMBLE}\n\n${passNote(pass)}${chunkNote(c, k)} It is your entire input.`,
  },
  {
    id: 'edge-case-hunter',
    active: true,
    prompt: (pass, c, k) => `${PREAMBLE}\n\n${passNote(pass)}Instruction file: ${A.prompts_dir}/edge-case-hunter.md\n` +
      `claims_file (leave unread until your instructions call for it): ${A.claims_file || '(none)'}\n` +
      `Review content: ${chunkNote(c, k)}\n${CODE_ACCESS}`,
  },
  {
    id: 'verification-gap',
    active: true,
    prompt: (pass, c, k) => `${PREAMBLE}\n\n${passNote(pass)}Instruction file: ${A.prompts_dir}/verification-gap.md\n` +
      `Review content: ${chunkNote(c, k)}\n${CODE_ACCESS}`,
  },
  {
    id: 'acceptance-auditor',
    active: Boolean(A.spec_file),
    prompt: (pass, c, k) => `${PREAMBLE}\n\n${passNote(pass)}Spec file: ${A.spec_file}\n${chunkNote(c, k)}\n${CODE_ACCESS}`,
  },
]

// ---- execution stages: each runs once over the whole diff and needs the story stack. They run
// one at a time (test runs share the stack's test database; a file write can restart a live-synced
// service), concurrently with the read-only review layers. ----
const list = v => (Array.isArray(v) ? v : []).map(p => String(p).replace(/\\/g, '/'))
const SOURCE_PY = list(A.backend_sources)
const TEST_PY = list(A.backend_tests)
const PBT_PY = list(A.pbt_sources)
const UI_SOURCE = list(A.ui_sources)
const UI_FILES = new Set(UI_CHUNKS.flatMap(c => list(c.files)))
const lineCount = chunks => chunks.reduce((n, c) => n + Number(c.lines || 0), 0)
const TOTAL_LINES = lineCount(CHUNKS)
const diffNote = chunks => `Diff files: ${chunks.map(c => `${c.path} (${c.lines} lines; Read calls offset/limit ${pageCalls(c)})`).join('; ')}. ` +
  `Read ALL of them. ${READ_RULE} Set diff_lines_read to the total number of lines you read. ` +
  `Each hunk line starts with its line number in the source file; cite locations as file:<that number>.`
const wholeDiffNote = diffNote(CHUNKS)
const stackSkip = !A.stack_project_name ? 'no stack_project_name'
  : A.snapshot_dir ? 'snapshot mode (the worktree is being mutated)' : ''
const stageHead = `story_id: ${A.story_id}\nworktree_path: ${A.worktree}\nstack_project_name: ${A.stack_project_name}\nround: ${A.round}\n`
const titleNote = UI_TITLES.length
  ? `Test titles the diff adds or changes (${UI_TITLES.length}); return a \`tests\` entry for each, the title copied exactly as this JSON array gives it:\n${JSON.stringify(UI_TITLES, null, 1)}\n`
  : 'Test titles the diff adds or changes (0): none; return `tests` as [].\n'
const viewportNote = UI_VIEWPORTS.length
  ? `Viewports the diff's tests set (${UI_VIEWPORTS.length}); return a \`viewports\` entry for each, the viewport copied exactly as this JSON array gives it, with \`state_ids\` the ids in \`states\` of the layout states you check or place at that viewport (at least one):\n${JSON.stringify(UI_VIEWPORTS, null, 1)}\n`
  : 'Viewports the diff\'s tests set (0): none; return `viewports` as [].\n'
const acNote = SPEC_ACS.length
  ? `Spec ACs (${SPEC_ACS.length}); return an \`acs\` entry for each, the id copied exactly as this JSON array gives it, with \`state_ids\` the ids in \`states\` of the states that AC describes, or \`state_ids\` as [] and a \`reason\` when it describes no screen state:\n${JSON.stringify(SPEC_ACS, null, 1)}\n`
  : 'Spec ACs (0): none; return `acs` as [].\n'
// A branch that changes the profile is verified against its own copy in every round (review_stage.py resolves
// ui_profile to the worktree copy when branch_files list it).
const profileNote = `Profile file: ${A.ui_profile}\n` +
  (A.ui_profile_from_branch ? "This branch changes the UI profile; this copy is the project's UI profile for the round.\n" : '')
const PROFILE_NAME = 'the Profile file named above'
// The stack is seeded once per round, before the ui-verifier's first attempt (layered-review pipeline Step 1), so a
// retry runs on whatever that attempt left. Appended to the retry's prompt only (`runStage`), followed by the line
// naming why the first attempt ended, which the note's rule keys on: an attempt that ended early may have destroyed
// nothing, so an absent seeded state is not attributed to it.
const UI_RETRY_NOTE = `Retry: this stack was seeded once, before this round's first ui-verifier attempt, and is not re-seeded for this one. ` +
  `Data the first attempt changed stays changed. The last line, \`First attempt ended: <reason>\`, says how that attempt ended and where a ` +
  `seeded state that is absent now goes: after \`agent error: <message>\` or \`no structured report\` the attempt may have ended before it acted, so the state goes ` +
  `to \`unreached\` with the reason "absent on a retry after an early first-attempt failure; seed regression not ruled out"; after any other reason ` +
  `(\`read X of N diff lines\`, an accounting error in its report) the attempt completed its run, so a seeded state that is absent (for example a ` +
  `record it deleted) goes to \`unreached\` with the reason "changed by this round's first ui-verifier attempt". Either way the entry carries ` +
  `one \`methods_tried\` item, \`method\` \`rule\`, and the state is never a deviation finding.`
const STAGES = [
  {
    id: 'ui-verifier',
    skip: !A.ui_base_url ? 'no ui_base_url' : !A.ui_profile ? 'no UI profile (config review.ui_profile)'
      : A.snapshot_dir ? 'snapshot mode (the worktree is being mutated)'
      : !UI_SOURCE.length ? 'no changed UI source' : !A.spec_file ? 'no spec file' : '',
    schema: UI_SCHEMA,
    lines: lineCount(UI_CHUNKS),
    retry_note: UI_RETRY_NOTE,
    prompt: () => `${PREAMBLE}\n\n${stageHead}ui_base_url: ${A.ui_base_url}\nSpec file: ${A.spec_file}\n${profileNote}` +
      `Changed UI files: ${UI_SOURCE.join(', ')}\n${titleNote}${viewportNote}${acNote}` +
      `To create test data through this story stack's UI as an acting actor of ` +
      `${PROFILE_NAME}, within that actor's scope, when a state to check needs it is part of this task, authorized by the owner; the story ` +
      `stack is disposable and wiped at cleanup. The profile's observe-only actors are read-only. Routing this page's own WebSocket traffic, and the ` +
      `sockets of the second browser context (uivSecondHold, uivSecondRelease, uivSecondDrop, uivSecondSend), with Playwright, as ui-verifier.md ` +
      `describes, is part of the task and changes no stored data: hold, drop, inject, HTTP delay and fail, a held pointer press released off its element (uivPress, uivPressRelease), ending a held request unsent (uivRouteRelease with an outcome), delivering one held response at a time (uivRouteReleaseOne), rewriting fields of a real frame (uivRewrite), calling an app hook the profile names (uivHook), and a second login in a second browser ` +
      `context. The API and the database stay off limits.\n` +
      `Your diff is the UI part of the change (config review.ui_paths). ` +
      `${diffNote(UI_CHUNKS)}\n${CODE_ACCESS}`,
  },
  {
    id: 'pbt-writer',
    skip: stackSkip || (!PBT_PY.length ? 'no changed pure-function module (config review.pbt_source_regex)' : ''),
    schema: PBT_SCHEMA,
    lines: TOTAL_LINES,
    prompt: () => `Report findings only — do NOT apply patches — write nothing but the untracked property test files your ` +
      `instructions allow — ${CLASSIFY}\n\n${stageHead}` +
      `Spec file: ${A.spec_file || '(none)'}\nChanged modules: ${PBT_PY.join(', ')}\n${wholeDiffNote}\n${CODE_ACCESS}`,
  },
  {
    id: 'test-adequacy',
    skip: stackSkip || (!SOURCE_PY.length ? 'no changed source module (config review.mutation / backend_source_regex)'
      : !TEST_PY.length ? 'no changed test to judge' : ''),
    schema: FINDINGS_SCHEMA,
    lines: TOTAL_LINES,
    prompt: () => `${PREAMBLE}\n\n${stageHead}diff_range: ${A.diff_range}\nChanged modules: ${SOURCE_PY.join(', ')}\n` +
      `Changed tests: ${TEST_PY.join(', ')}\n${wholeDiffNote}\n${CODE_ACCESS}`,
  },
]

// A ui-verifier report places each id of its `states` exactly once: in `checked_ids`, or as the `state_id` of an
// `unreached` or `out_of_scope` entry. Returns the lines of the ids placed nowhere, placed more than once (in one
// list or in several) and placed without being in `states`, the dispatched `titles` that no `tests` entry names, the
// dispatched `viewports` that no `viewports` entry names, the dispatched `acs` that no `acs` entry names, or `error`
// for a list it cannot read. A `tests`, `viewports` or `acs` entry maps to states and is no placement: a state several
// entries name stays singly placed, and an entry whose state id is absent from `states` closes `unknown_ids`.
// An `acs` entry may name no state, with a reason. An `unreached` entry carries a non-empty `methods_tried` of
// `{method, stopped_by}` (`method` in `UNREACHED_METHODS`, `stopped_by` text with a non-blank character), else `error`.
// Mirrored by `state_accounting`, `untested_titles`, `unplaced_viewports`, `unmapped_acs` and `_unreached_list` in
// review_findings_render.py, which refuses a run whose lists differ from its own.
function uiAccounting(r, { titles, viewports, acs }) {
  const entries = [['unreached', r.unreached], ['out_of_scope', r.out_of_scope]]
  if (![r.states, r.checked_ids, r.tests, r.viewports, r.acs, ...entries.map(e => e[1])].every(Array.isArray))
    return { error: 'states, checked_ids, tests, viewports, acs, unreached and out_of_scope must be lists' }
  const known = new Map()
  for (const s of r.states) {
    if (!s || typeof s.id !== 'string' || !s.id || typeof s.state !== 'string' || !s.state)
      return { error: 'a states entry has no id or no state' }
    if (known.has(s.id)) return { error: `states repeats the id ${s.id}` }
    known.set(s.id, s.state)
  }
  if (!r.checked_ids.every(id => typeof id === 'string')) return { error: 'checked_ids holds a value that is not a string' }
  for (const t of r.tests)
    if (!t || typeof t.title !== 'string' || !t.title || typeof t.state_id !== 'string' || !t.state_id)
      return { error: 'a tests entry has no title or no state_id' }
  const isText = v => typeof v === 'string' && v
  const areIds = v => Array.isArray(v) && v.every(isText)
  const hasTried = e => Array.isArray(e.methods_tried) && e.methods_tried.length > 0 &&
    e.methods_tried.every(m => m && UNREACHED_METHODS.includes(m.method) && isText(m.stopped_by) && m.stopped_by.trim())
  for (const v of r.viewports)
    if (!v || !isText(v.viewport) || !areIds(v.state_ids) || !v.state_ids.length)
      return { error: 'a viewports entry has no viewport or no state_ids' }
  for (const a of r.acs) {
    if (!a || !isText(a.ac) || !areIds(a.state_ids) || (a.reason !== undefined && typeof a.reason !== 'string'))
      return { error: 'an acs entry has no ac, no state_ids list or a reason that is not text' }
    if (!a.state_ids.length && !(a.reason || '').trim()) return { error: `the acs entry for ${a.ac} has no state_ids and no reason` }
  }
  const placed = new Map()
  const place = (id, where, state) => {
    const p = placed.get(id) || { where: [], state: '' }
    p.where.push(where)
    if (!p.state && typeof state === 'string') p.state = state
    placed.set(id, p)
  }
  r.checked_ids.forEach(id => place(id, 'checked_ids'))
  for (const [where, list] of entries)
    for (const e of list) {
      if (!e || typeof e.state_id !== 'string' || !e.state_id) return { error: `an ${where} entry has no state_id` }
      if (where === 'unreached' && !hasTried(e))
        return { error: `the unreached entry for ${e.state_id} has no valid methods_tried (a non-empty list of {method, stopped_by}: ` +
          `method one of ${UNREACHED_METHODS.join(', ')}, stopped_by text)` }
      place(e.state_id, where, e.state)
    }
  const line = (id, text, where) => [id, text, where && `placed in ${where.join(', ')}`].filter(Boolean).join(' — ')
  const text = id => known.get(id) || placed.get(id).state
  const mapped = new Set(r.tests.map(t => t.title))
  const mappedViewports = new Set(r.viewports.map(v => v.viewport))
  const mappedAcs = new Set(r.acs.map(a => a.ac))
  return {
    unaccounted: [...known].filter(([id]) => !placed.has(id)).map(([id, state]) => line(id, state)),
    double_placed: [...placed].filter(([, p]) => p.where.length > 1).map(([id, p]) => line(id, text(id), p.where)),
    unknown_ids: [
      ...[...placed].filter(([id]) => !known.has(id)).map(([id, p]) => line(id, text(id), p.where)),
      ...r.tests.filter(t => !known.has(t.state_id)).map(t => line(t.state_id, t.title, ['tests'])),
      ...r.viewports.flatMap(v => v.state_ids.filter(id => !known.has(id)).map(id => line(id, v.viewport, ['viewports']))),
      ...r.acs.flatMap(a => a.state_ids.filter(id => !known.has(id)).map(id => line(id, a.ac, ['acs']))),
    ],
    untested_titles: titles.filter(t => !mapped.has(t)),
    unplaced_viewports: viewports.filter(v => !mappedViewports.has(v)),
    unmapped_acs: acs.filter(a => !mappedAcs.has(a)),
  }
}

async function runStage(s) {
  let reason = 'no report'
  for (let attempt = 1; attempt <= 2; attempt++) {
    const journalLabel = attempt === 1 ? s.id : `${s.id} (retry)`
    const prompt = attempt > 1 && s.retry_note
      ? `${s.prompt()}\n${s.retry_note}\nFirst attempt ended: ${String(reason).replace(/\s+/g, ' ').trim()}` : s.prompt()
    let r = null
    try {
      r = await agent(prompt, { label: journalLabel, phase: 'Execute', agentType: s.id, effort: EFFORT[s.id], schema: s.schema })
    } catch (e) {
      reason = `agent error: ${String((e && e.message) || e).slice(0, 200)}`
      continue
    }
    if (!r || !Array.isArray(r.findings)) { reason = 'no structured report'; continue }
    if (!(Number(r.diff_lines_read) >= s.lines)) { reason = `read ${r.diff_lines_read} of ${s.lines} diff lines`; continue }
    const accounting = s.id === 'ui-verifier' ? uiAccounting(r, { titles: UI_TITLES, viewports: UI_VIEWPORTS, acs: SPEC_ACS }) : null
    if (accounting && accounting.error) { reason = accounting.error; continue }
    return { label: s.id, journal_label: journalLabel, layer: s.id, pass: 1, chunk: 0, failed: false, reason: '', findings: r.findings,
      ...(accounting ? { ui_accounting: accounting } : {}) }
  }
  return { label: s.id, journal_label: null, layer: s.id, pass: 1, chunk: 0, failed: true, reason, findings: [] }
}

async function runStages() {
  const out = []
  for (const s of STAGES) if (!s.skip) out.push(await runStage(s))
  return out
}

const labelOf = (layerId, pass, k) =>
  layerId + (PASSES > 1 ? `#${pass}` : '') + (CHUNKS.length > 1 ? `@${k + 1}` : '')

async function runLayer(layer, pass, c, k) {
  const label = labelOf(layer.id, pass, k)
  // A layer counts as missing only after a retry. A thrown agent (schema retries exhausted),
  // a null return, a schema-less return and a partial read of the chunk are all silence,
  // and silence is never a pass.
  let reason = 'no report'
  for (let attempt = 1; attempt <= 2; attempt++) {
    const journalLabel = attempt === 1 ? label : `${label} (retry)`
    let r = null
    try {
      r = await agent(layer.prompt(pass, c, k), {
        label: journalLabel,
        phase: 'Review',
        agentType: layer.id,
        effort: EFFORT[layer.id],
        schema: FINDINGS_SCHEMA,
      })
    } catch (e) {
      reason = `agent error: ${String((e && e.message) || e).slice(0, 200)}`
      continue
    }
    if (!r || !Array.isArray(r.findings)) { reason = 'no structured report'; continue }
    if (!(Number(r.diff_lines_read) >= c.lines)) { reason = `read ${r.diff_lines_read} of ${c.lines} diff lines`; continue }
    return { label, journal_label: journalLabel, layer: layer.id, pass, chunk: k + 1, failed: false, reason: '', findings: r.findings }
  }
  return { label, journal_label: null, layer: layer.id, pass, chunk: k + 1, failed: true, reason, findings: [] }
}

for (const l of [...LAYERS, ...STAGES]) if (!EFFORT[l.id]) throw new Error(`review: no EFFORT entry for ${l.id}`)
const skipped = LAYERS.filter(l => !l.active).map(l => l.id)
  .concat(STAGES.filter(s => s.skip).map(s => `${s.id} (${s.skip})`))
if (skipped.length) log(`Skipped: ${skipped.join(', ')}`)
if (!CHUNKS.length) throw new Error('review: args.diff_chunks is empty — nothing to review')
// ui_chunks cover diff_range or more, so every UI source file the stage names must be in them.
const unstagedUi = UI_SOURCE.filter(p => !UI_FILES.has(p))
if (A.ui_base_url && unstagedUi.length)
  throw new Error(`review: args.ui_chunks lack changed UI source ${unstagedUi.join(', ')}`)
if (!STAGES.find(s => s.id === 'ui-verifier').skip && !ACS_OK) throw new Error('review: args.spec_acs is not a list of AC ids')
const jobs = []
for (let pass = 1; pass <= PASSES; pass++)
  for (const l of LAYERS.filter(x => x.active))
    CHUNKS.forEach((c, k) => jobs.push({ l, pass, c, k }))
// Barrier: dedupe needs every layer's findings at once. Results are matched to jobs by
// index, so a job whose thunk resolved to null is still reported as failed.
const [settled, staged] = await Promise.all([
  parallel(jobs.map(j => () => runLayer(j.l, j.pass, j.c, j.k))),
  runStages(),
])
const layers = jobs.map((j, i) => settled[i] || {
  label: labelOf(j.l.id, j.pass, j.k), journal_label: null, layer: j.l.id, pass: j.pass, chunk: j.k + 1,
  failed: true, reason: 'layer run returned nothing', findings: [],
}).concat(staged)
const failedLayers = layers.filter(l => l.failed).map(l => l.label)
const failedReasons = Object.fromEntries(layers.filter(l => l.failed).map(l => [l.label, l.reason]))
if (failedLayers.length) log(`Layers with no report after retry: ${failedLayers.map(x => `${x} (${failedReasons[x]})`).join(', ')}`)

// ---- dedupe in code: same file, same class — `gap`, `property`, or a defect kind (each layer stamps its own kind,
// so kind equality would never merge across layers), line ranges within 3 lines, never two findings of
// one layer run, and >= half the shorter text's non-stopword tokens shared. A raw id is
// `<layer label>.<1-based index>`; the renderer resolves it against the journal, so the
// evidence text built here only feeds the verifier prompt. ----
const REWRITES = (A.path_rewrites || []).filter(r => Array.isArray(r) && r.length === 2).map(([x, y]) => [new RegExp(x), y])
const escapeRx = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const WT_DIR_RX = A.worktrees_dir ? new RegExp(`^.*?${escapeRx(A.worktrees_dir)}/[^/]+/`) : null
const cutWorktree = p => (WT_DIR_RX ? p.replace(WT_DIR_RX, '') : p)
// Mirrored by `_norm_path` in review_findings_render.py.
const normPath = p => REWRITES.reduce((acc, [x, y]) => acc.replace(x, y), cutWorktree(String(p).trim()
  .replace(/[`'"]/g, '')
  .replace(/\\/g, '/')
  .replace(/^[ab]\//, '')))
  .replace(/^\.?\//, '')
function parseLoc(loc) {
  const m = /^(.+?):(\d+)(?:-(\d+))?/.exec(String(loc || '').replace(/[`'"]/g, '').trim())
  return m ? { file: normPath(m[1]), start: Number(m[2]), end: Number(m[3] || m[2]) } : null
}
const STOP = new Set(['the', 'and', 'for', 'with', 'that', 'this', 'from', 'are', 'not', 'but', 'when', 'into',
  'its', 'has', 'have', 'was', 'will', 'can', 'does', 'should', 'would', 'which', 'than', 'then', 'there',
  'their', 'been', 'also', 'any', 'all', 'one', 'only', 'via', 'per', 'out', 'use', 'used', 'uses', 'may',
  'missing', 'line', 'lines', 'code', 'file', 'function', 'value', 'case'])
const tokens = s => new Set((String(s).toLowerCase().match(/[a-z0-9_]{3,}/g) || []).filter(t => !STOP.has(t)))
function similar(a, b) {
  const x = tokens(a), y = tokens(b)
  let shared = 0
  x.forEach(t => { if (y.has(t)) shared++ })
  return shared / Math.max(1, Math.min(x.size, y.size)) >= 0.5
}
const kindClass = k => (k === 'gap' || k === 'property' ? k : 'defect')
const raw = []
for (const l of layers) l.findings.forEach((f, i) => raw.push({ ...f, raw_id: `${l.label}.${i + 1}`, source: l.label, loc: parseLoc(f.location) }))
const groups = []
for (const f of raw) {
  const g = f.loc && groups.find(g => g.loc && g.loc.file === f.loc.file && kindClass(g.kind) === kindClass(f.kind) &&
    !g.labels.has(f.source) &&
    f.loc.start <= g.loc.end + 3 && g.loc.start <= f.loc.end + 3 &&
    similar(`${g.title} ${g.claim}`, `${f.title} ${f.claim}`))
  if (g) {
    g.labels.add(f.source)
    g.sources.push(f.raw_id)
    g.evidence.push({ source: f.raw_id, text: `${f.claim} — ${f.evidence}` })
    g.recs.push(f.recommended)
  } else {
    groups.push({ title: f.title, location: f.location, loc: f.loc, claim: f.claim, kind: f.kind,
      confidence: f.confidence, labels: new Set([f.source]), sources: [f.raw_id], recs: [f.recommended],
      evidence: [{ source: f.raw_id, text: f.evidence }], carried: [] })
  }
}
// Carried findings of halted rounds join the first group the in-round rule matches, one per halted
// section per group (`carried:R<round>` in `labels`); an unmatched one heads a group under its own id.
// The renderer reads a carried finding's text from the findings file; the text here feeds dedupe and prompts.
const ownGroups = groups.length
for (const c of CARRIED) {
  const label = `carried:R${c.round}`, src = `carried:${c.id}`, loc = parseLoc(c.location)
  const g = loc && groups.find(g => g.loc && g.loc.file === loc.file && kindClass(g.kind) === kindClass(c.kind) &&
    !g.labels.has(label) &&
    loc.start <= g.loc.end + 3 && g.loc.start <= loc.end + 3 &&
    similar(`${g.title} ${g.claim}`, `${c.title} ${c.claim}`))
  const evidence = { source: src, text: `${c.claim} (round ${c.round})` }
  const recs = String(c.recommended).split(' / ')
  if (g) {
    g.labels.add(label)
    g.sources.push(src)
    g.evidence.push(evidence)
    g.recs.push(...recs)
    g.carried.push(c)
  } else {
    groups.push({ id: c.id, title: c.title, location: c.location, loc, claim: c.claim, kind: c.kind,
      confidence: undefined, labels: new Set([label]), sources: [src], recs, evidence: [evidence], carried: [c] })
  }
}
const carriedMerged = CARRIED.filter(c => groups.slice(0, ownGroups).some(g => g.carried.includes(c))).length
const findings = groups.map((g, i) => ({
  id: g.id || `R${A.round}-${i + 1}`, title: g.title, location: g.location, claim: g.claim, kind: g.kind,
  confidence: g.confidence, sources: g.sources, evidence: g.evidence, carried: g.carried,
  // Layer disagreement is preserved, never averaged away: the human sees every recommendation.
  recommended: [...new Set(g.recs)].join(' / '),
}))
log(`${raw.length} raw findings${CARRIED.length ? ` + ${CARRIED.length} carried` : ''} → ${findings.length} after dedupe`)

// ---- verify: batches of <= 5 findings grouped by file; every assigned id must come back ----
phase('Verify')
function batchByFile(list) {
  const byFile = {}
  for (const f of list) {
    const key = parseLoc(f.location)?.file || '(unlocated)'
    ;(byFile[key] = byFile[key] || []).push(f)
  }
  const out = []
  for (const group of Object.values(byFile)) for (let i = 0; i < group.length; i += 5) out.push(group.slice(i, i + 5))
  return out
}
const batches = batchByFile(findings)

const INPUTS = `Inputs you may need: diff chunks ${CHUNKS.map(c => c.path).join(', ')} (removed code exists only there — ` +
  `read the hunk for a deletion finding); commit messages ${A.claims_file || '(none)'} (for a claim finding); ` +
  `spec ${A.spec_file || '(none)'} (for an ac finding); ` +
  (A.quality_bar_doc
    ? `quality bar ${A.quality_bar_doc} (read it in full before your first bar_impact line).`
    : "no quality bar is configured: every non-REFUTED bar_impact is 'n/a (no quality bar configured)'.")
// A carried finding can sit in any findings file of the story, not only the one this round writes, under any
// spelling of the story id that review_ledger.variants reads (dashed and dotted).
const STORY_ID = String(A.story_id)
const FINDINGS_DIR = String(A.findings_file).replace(/[^\/\\]*$/, '')
const FINDINGS_FILES = [...new Set([STORY_ID, STORY_ID.replace(/\./g, '-'), STORY_ID.replace(/-/g, '.')])]
  .map(id => `\`${FINDINGS_DIR}${id}-code-review-findings*.md\``).join(' or ')
const verifyPrompt = batch => `${PREAMBLE}\n\n${CODE_ACCESS}\n${INPUTS}\n\n` +
  'Findings assigned to you — return exactly one verdict per id:\n\n' +
  batch.map(f => `### ${f.id} — ${f.title}\n- kind: ${f.kind}\n- location: ${f.location}\n- claim: ${f.claim}\n- layer evidence:\n` +
    f.evidence.map(e => `  - [${e.source}] ${e.text}`).join('\n') +
    f.carried.map(c => `\n- carried from halted round ${c.round} as ${c.id}: its location reflects round ${c.round}'s tree and the branch has changed since — ` +
      `find the code the claim describes and cite the current file:line; its full layer evidence is the \`#### ${c.id} — \` block under ` +
      `\`## Review round ${c.round}\` in whichever of the story's findings files (${FINDINGS_FILES}) holds that round ` +
      '(Claim and Layer evidence lines).').join('')).join('\n\n')

// The first verdict for an id wins, within a response and across the retry; `by` is the
// journal label of the call that returned it, so the renderer can look the verdict up.
async function verifyBatch(batch, baseLabel) {
  const got = {}
  let pending = batch
  for (let attempt = 1; attempt <= 2 && pending.length; attempt++) {
    const label = `${baseLabel}${attempt > 1 ? ' (retry)' : ''}`
    let r = null
    try {
      r = await agent(verifyPrompt(pending), {
        label,
        phase: 'Verify',
        agentType: 'findings-verifier',
        effort: EFFORT['findings-verifier'],
        schema: VERDICT_SCHEMA,
      })
    } catch (e) {
      r = null
    }
    for (const v of (r && r.verdicts) || [])
      if (!got[v.id] && pending.some(f => f.id === v.id)) got[v.id] = { ...v, by: label }
    pending = pending.filter(f => !got[f.id])
  }
  return got
}
const firstMaps = await parallel(batches.map((b, i) => () => verifyBatch(b, `verify batch ${i + 1}`)))
const votes = {}
for (const m of firstMaps.filter(Boolean)) for (const [id, v] of Object.entries(m)) votes[id] = [v]

// ---- votes (auto mode): a finding the evaluator may patch, or one that blocks the quality bar,
// needs a majority of three verifiers. Each vote batch is a fresh, independent verifier call. ----
if (A.mode === 'auto') {
  const contested = findings.filter(f => {
    const v = votes[f.id] && votes[f.id][0]
    return v && v.verdict !== 'REFUTED' && (/\bpatch\b/.test(f.recommended) || /^BLOCKING/.test(v.bar_impact || '') ||
      f.carried.some(c => /^BLOCKING/.test(c.bar_impact)))
  })
  if (contested.length) {
    phase('Vote')
    const vb = batchByFile(contested)
    const extra = await parallel([2, 3].flatMap(n => vb.map((b, i) => () => verifyBatch(b, `vote ${n} batch ${i + 1}`))))
    for (const m of extra.filter(Boolean)) for (const [id, v] of Object.entries(m)) votes[id].push(v)
    log(`${contested.length} findings put to a 3-verifier vote`)
  }
}

// Majority of the votes cast: CONFIRMED or REFUTED needs two; anything else is UNVERIFIABLE.
// A single BLOCKING vote is kept as a minority line, never dropped. Mirrored by `decide` in
// review_findings_render.py.
function decide(vs) {
  if (vs.length === 1) {
    const v = vs[0]
    return { verdict: v.verdict, evidence: v.evidence, bar_impact: v.bar_impact, votes: [v.verdict], by: [v.by] }
  }
  const n = x => vs.filter(v => v.verdict === x).length
  const verdict = n('CONFIRMED') >= 2 ? 'CONFIRMED' : n('REFUTED') >= 2 ? 'REFUTED' : 'UNVERIFIABLE'
  const agree = vs.filter(v => v.verdict === verdict)
  const evidence = agree.length ? agree[0].evidence : `split vote — ${vs.map(v => `${v.verdict}: ${v.evidence}`).join(' | ')}`
  const blocking = vs.filter(v => /^BLOCKING/.test(v.bar_impact || ''))
  const bar_impact = verdict === 'REFUTED' ? 'n/a'
    : blocking.length >= 2 ? blocking[0].bar_impact
    : blocking.length === 1 ? `BLOCKING (minority, 1 of ${vs.length} votes)${blocking[0].bar_impact.slice('BLOCKING'.length)}`
    : (agree[0] || vs[0]).bar_impact
  return { verdict, evidence, bar_impact, votes: vs.map(v => v.verdict), by: vs.map(v => v.by) }
}
// A carried finding's halted BLOCKING is never lost (a bar-blocking finding is never deferred or dismissed
// blocker): the first carried member with one supplies the round and the line. A REFUTED verdict
// reads UNVERIFIABLE with the split evidence; a verdict whose impact is not BLOCKING gets the minority
// line. Mirrored by `apply_halted_blocking` in review_findings_render.py.
function keepHaltedBlocking(f, d) {
  const hb = f.carried.find(c => /^BLOCKING/.test(c.bar_impact))
  if (!hb) return d
  const line = `BLOCKING (minority, halted round ${hb.round})${hb.bar_impact.replace(/^BLOCKING(\s*\([^)]*\))?/, '')}`
  if (d.verdict === 'REFUTED')
    return { ...d, verdict: 'UNVERIFIABLE', bar_impact: line,
      evidence: `split across rounds — halted round ${hb.round}: ${hb.verdict} | round ${A.round}: REFUTED — ${d.evidence}` }
  return /^BLOCKING/.test(d.bar_impact || '') ? d : { ...d, bar_impact: line }
}
const unverified = []
for (const f of findings) {
  if (votes[f.id]) f.verification = keepHaltedBlocking(f, decide(votes[f.id]))
  else unverified.push(f.id)
}
if (unverified.length) log(`No verifier verdict for: ${unverified.join(', ')}`)

// ---- repro: a failing test per non-refuted runtime finding, written untracked into the
// worktree. One batch at a time: every run shares the stack's test database, and a file
// write can restart a live-synced service. ----
const reproSkipped = !A.stack_project_name ? 'no stack_project_name'
  : A.snapshot_dir ? 'snapshot mode (the worktree is being mutated)' : ''
// REPRODUCED counts only when an assertion in the test body failed: an import error, a wrong
// label or an unsynced file fails the run too and proves nothing. Mirrored in the renderer.
const reproStatus = r => (r.status === 'REPRODUCED' && r.failure_kind !== 'assertion' ? 'BLOCKED' : r.status)
const reproPrompt = batch => `Write one minimal failing test per finding below that demonstrates the claimed defect, run them, ` +
  `and report one result per id. Leave the test files in place, untracked.\n` +
  `story_id: ${A.story_id}\nworktree_path: ${A.worktree}\nstack_project_name: ${A.stack_project_name}\nround: ${A.round}\n` +
  `${CODE_ACCESS}\n${INPUTS}\n\n` +
  (batch.some(f => f.carried.length)
    ? 'Name each repro file from its finding id (R1-34 → repro_r1_34.test.jsx, test_repro_r1_34.py or repro-r1-34.spec.js).\n\n' : '') +
  'Findings — return exactly one result per id:\n\n' +
  batch.map(f => `### ${f.id} — ${f.title}\n- kind: ${f.kind}\n- location: ${f.location}\n- claim: ${f.claim}\n` +
    `- verification: ${f.verification.verdict} — ${f.verification.evidence}`).join('\n\n')
// A falsified property already carries its failing test: that test is the finding's repro.
const rawById = Object.fromEntries(raw.map(r => [r.raw_id, r]))
for (const f of findings) {
  const src = rawById[f.sources[0]]
  if (f.kind === 'property' && src)
    f.repro = { status: reproStatus({ status: 'REPRODUCED', failure_kind: src.failure_kind }), failure_kind: src.failure_kind,
      test_path: src.test_path, test_id: src.test_id, tail: src.tail, by: null, source: f.sources[0] }
}
const repro = {}
// A carried-only property finding has no pbt source this round: its falsifying test was deleted by the clean-up.
const reproTargets = findings.filter(f => f.verification && f.verification.verdict !== 'REFUTED' && f.kind !== 'gap' &&
  (f.kind !== 'property' || !rawById[f.sources[0]]))
if (reproSkipped) log(`Repro stage skipped: ${reproSkipped}`)
else if (reproTargets.length) {
  phase('Repro')
  const rb = batchByFile(reproTargets)
  for (let i = 0; i < rb.length; i++) {
    const label = `repro batch ${i + 1}`
    let r = null
    try {
      r = await agent(reproPrompt(rb[i]), {
        label,
        phase: 'Repro',
        agentType: 'repro-writer',
        effort: EFFORT['repro-writer'],
        schema: REPRO_SCHEMA,
      })
    } catch (e) {
      r = null
    }
    for (const x of (r && r.results) || [])
      if (!repro[x.id] && rb[i].some(f => f.id === x.id)) repro[x.id] = { ...x, status: reproStatus(x), by: label }
  }
}
for (const f of findings) if (repro[f.id]) f.repro = repro[f.id]
const reproCell = f => (f.repro ? `${f.repro.status}${f.repro.test_path ? ` ${f.repro.test_path}` : ''}` : '—')

const uiRun = layers.find(l => l.ui_accounting)
const row = f => ({
  id: f.id, sources: f.sources.join(', '), location: f.location, title: f.title,
  verdict: f.verification ? f.verification.verdict : 'UNVERIFIED',
  bar_impact: f.verification ? f.verification.bar_impact : 'MISSING',
  votes: f.verification ? f.verification.votes.join(', ') : '',
  repro: reproCell(f),
  recommended: f.recommended,
})
// The run file keeps this return value and the journal keeps every agent's result;
// review_findings_render.py rebuilds the findings document from the two, so no
// agent copies review text. `record` holds structure only.
return {
  story_id: A.story_id, round: A.round, mode: A.mode, passes: PASSES, date: A.date,
  diff_range: A.diff_range, delta: A.delta, findings_file: A.findings_file,
  counts: {
    raw: raw.length, deduped: findings.length,
    confirmed: findings.filter(f => f.verification?.verdict === 'CONFIRMED').length,
    unverifiable: findings.filter(f => f.verification?.verdict === 'UNVERIFIABLE').length,
    refuted: findings.filter(f => f.verification?.verdict === 'REFUTED').length,
    unverified: unverified.length,
    blocking: findings.filter(f => /^BLOCKING/.test(f.verification?.bar_impact || '')).length,
    reproduced: findings.filter(f => f.repro?.status === 'REPRODUCED').length,
    carried: CARRIED.length, carried_merged: carriedMerged,
  },
  failed_layers: failedLayers,
  failed_reasons: failedReasons,
  skipped_layers: skipped,
  ...(uiRun ? { ui_unaccounted: uiRun.ui_accounting.unaccounted, ui_double_placed: uiRun.ui_accounting.double_placed,
    ui_unknown_ids: uiRun.ui_accounting.unknown_ids, ui_untested_titles: uiRun.ui_accounting.untested_titles,
    ui_unplaced_viewports: uiRun.ui_accounting.unplaced_viewports, ui_unmapped_acs: uiRun.ui_accounting.unmapped_acs,
    ui_test_titles: UI_TITLES, ui_test_viewports: UI_VIEWPORTS, ui_spec_acs: SPEC_ACS } : {}),
  unverified,
  repro_skipped: reproSkipped,
  triage: findings.filter(f => f.verification?.verdict !== 'REFUTED').map(row),
  refuted: findings.filter(f => f.verification?.verdict === 'REFUTED')
    .map(f => ({ id: f.id, title: f.title, location: f.location, why: String(f.verification.evidence).slice(0, 200) })),
  record: {
    raw_count: raw.length,
    carried_input: CARRIED.map(c => ({ id: c.id, round: c.round })),
    layers: layers.map(l => ({ label: l.label, failed: l.failed, reason: l.reason, journal_label: l.journal_label })),
    findings: findings.map(f => ({ id: f.id, sources: f.sources, verified_by: f.verification ? f.verification.by : null,
      repro_by: f.repro ? f.repro.by : null, ...(f.repro && f.repro.source ? { repro_source: f.repro.source } : {}),
      ...(f.carried.length ? { carried: f.carried.map(c => ({ id: c.id, round: c.round })) } : {}) })),
  },
}
