export const meta = {
  name: 'adversarial-review',
  description: 'Adversarial review of the uncommitted working-tree diff: 3 finders, dedup, severity-tiered refute votes with fable tiebreak on splits',
  whenToUse:
    'Before committing a slice: reviews the uncommitted diff against the spec and the slice issue. Pass args {issue: <github issue number>, leads: [<areas to probe>], specRefs: "<spec sections>"}. Proven recipe from slices 3 & 4 (found 1 critical + 3 majors on slice 3, 1 major + 5 on slice 4).',
  phases: [
    { title: 'Find', detail: '3 parallel finders: correctness (fable/high), spec (opus/high), test-gaps (opus/medium)' },
    { title: 'Dedup', detail: 'sonnet/medium merge-groups pass when ≥5 findings survive string dedup' },
    { title: 'Verify', detail: 'severity-tiered refute votes (opus/sonnet); fable/high tiebreak on split votes' },
  ],
}

const issue = args?.issue ?? null
const specRefs = args?.specRefs ?? 'ARCHITECTURE-v2.md — read §0 (invariants) plus every section the diff touches'
const leads = args?.leads ?? []

const CHANGESET = `
The change under review is the ENTIRE UNCOMMITTED working tree diff against HEAD in the Expensir repo (your working directory):
- Run \`git status --porcelain\` to see modified AND untracked files.
- Run \`git diff\` for the modified tracked files.
- Read every NEW untracked file in full — \`git diff\` does not show them.
Read any surrounding unchanged code you need for context.
`

const ISSUE = issue
  ? `The slice spec is GitHub issue #${issue}. Fetch it with \`gh issue view ${issue} --json title,body\` (plain \`gh issue view\` prints nothing in this environment — always use --json). Check the implementation against every acceptance criterion.`
  : 'No slice issue was given; review against the architecture spec alone.'

const LEADS = leads.length
  ? `\nAreas worth probing (leads, not conclusions — verify before reporting):\n${leads.map(l => `- ${l}`).join('\n')}\n`
  : ''

const REPORT_RULES = `
Report ONLY findings you have verified against the actual code — quote the relevant lines. Do NOT report: style nits, hypotheticals you could not trace to concrete code, things the spec explicitly accepts, or work explicitly deferred to later slices (unless the current code makes that later work impossible). Severity: critical = data corruption / wrong money math / security; major = real user-visible misbehavior or spec violation; minor = edge case or forward-compat flag. Your final output goes through the StructuredOutput tool; the findings array may be empty.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string', description: 'repo-relative path' },
          line: { type: 'integer' },
          title: { type: 'string', description: 'one-line defect statement' },
          claim: { type: 'string', description: 'precise claim: inputs/state -> wrong behavior, with code evidence' },
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
        },
        required: ['file', 'title', 'claim', 'severity'],
        additionalProperties: false,
      },
    },
  },
  required: ['findings'],
  additionalProperties: false,
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    refuted: { type: 'boolean', description: 'true if the finding is wrong, not a real problem, or out of scope for this slice' },
    reasoning: { type: 'string' },
  },
  required: ['refuted', 'reasoning'],
  additionalProperties: false,
}

const DEDUP_SCHEMA = {
  type: 'object',
  properties: {
    groups: {
      type: 'array',
      description: 'groups of finding indices that describe the same underlying defect; findings not in any group are distinct',
      items: { type: 'array', items: { type: 'integer' }, minItems: 2 },
    },
  },
  required: ['groups'],
  additionalProperties: false,
}

// Finder allocation: fable only where the task is genuinely hard (mental execution of
// async/race/money paths); spec cross-referencing and test-gap enumeration run on opus.
// Find is the recall stage, so efforts skew high.
const FINDERS = [
  {
    key: 'correctness',
    model: 'fable',
    effort: 'high',
    prompt: `You are an adversarial code reviewer with a CORRECTNESS/BUG lens reviewing an uncommitted slice of the Expensir repo (a Telegram expense-tracking bot, Python 3.12, SQLAlchemy async, uv toolchain).
${CHANGESET}
${ISSUE}
Hunt for real bugs: race conditions, idempotency violations, wrong SQL/ORM semantics, datetime/timezone bugs, money-math errors, state machines that can wedge, error paths that corrupt state, crash-window inconsistencies, Telegram API misuse (callback answer timeouts, 64-byte callback data, message edit semantics, InaccessibleMessage). Trace the actual code paths — run the code mentally end-to-end for every user-visible flow the diff adds or changes.
${LEADS}
${REPORT_RULES}`,
  },
  {
    key: 'spec',
    model: 'opus',
    effort: 'high',
    prompt: `You are an adversarial code reviewer with a SPEC-COMPLIANCE lens reviewing an uncommitted slice of the Expensir repo.
${CHANGESET}
${ISSUE}
Spec: ${specRefs}. Also read CONTEXT.md for vocabulary and docs/adr/ for locked decisions. Check the implementation against every normative statement in the touched spec sections and every acceptance criterion of the issue. Report any place where the code deviates from the spec or silently narrows/widens it.
${LEADS}
${REPORT_RULES}`,
  },
  {
    key: 'testgaps',
    model: 'opus',
    effort: 'medium',
    prompt: `You are an adversarial code reviewer with a TEST-GAPS lens reviewing an uncommitted slice of the Expensir repo.
${CHANGESET}
${ISSUE}
Read the diff's test files in full, plus the production code they cover. Identify behaviors in the new production code that NO test pins: untested guard branches, error paths, fallback chains, transport wiring. A test gap is only a finding if the untested behavior is load-bearing (spec acceptance criterion or plausible regression magnet) — name the exact missing test and what regression it would catch.
${REPORT_RULES}`,
  },
]

phase('Find')
log('Spawning 3 finders: correctness (fable/high), spec (opus/high), test-gaps (opus/medium)')
const found = (await parallel(FINDERS.map(f => () =>
  agent(f.prompt, { label: `find:${f.key}`, phase: 'Find', model: f.model, effort: f.effort, schema: FINDINGS_SCHEMA })
    .then(r => (r?.findings ?? []).map(x => ({ ...x, finder: f.key })))
))).filter(Boolean).flat()

log(`Finders returned ${found.length} raw findings`)

// First pass: free string-key dedup for near-identical titles.
const seen = new Map()
for (const f of found) {
  const key = f.file + '::' + (f.title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().slice(0, 80)
  if (!seen.has(key)) seen.set(key, f)
  else seen.get(key).finder += '+' + f.finder
}
let deduped = [...seen.values()]
log(`${deduped.length} findings after string dedup`)

if (deduped.length === 0) return { confirmed: [], killed: [], raw: 0 }

// Second pass: semantic dedup — paraphrases of the same root cause escape the string key
// and would waste verify votes. Only worth an agent when there are enough findings to collide.
const SEV_RANK = { critical: 0, major: 1, minor: 2 }
if (deduped.length >= 5) {
  phase('Dedup')
  const listing = deduped.map((f, i) =>
    `[${i}] (${f.severity}, finder: ${f.finder}) ${f.file}${f.line ? ':' + f.line : ''} — ${f.title}\n    ${f.claim}`
  ).join('\n')
  const res = await agent(`You are deduplicating code-review findings. Below is a numbered list of findings from independent reviewers of the same change. Identify groups of findings that describe the SAME underlying defect at the same root cause — even if worded differently, anchored to different lines, or framed under different lenses (e.g. one as a bug, one as a spec violation, one as a missing test for the same behavior). Do NOT group findings that merely touch the same file or the same function but describe different defects. Return groups of indices; findings that are unique appear in no group.

${listing}`,
    { label: 'dedup', phase: 'Dedup', model: 'sonnet', effort: 'medium', schema: DEDUP_SCHEMA })
  if (res?.groups?.length) {
    const drop = new Set()
    for (const g of res.groups) {
      const idxs = [...new Set(g)].filter(i => Number.isInteger(i) && i >= 0 && i < deduped.length && !drop.has(i))
      if (idxs.length < 2) continue
      idxs.sort((a, b) => (SEV_RANK[deduped[a].severity] ?? 3) - (SEV_RANK[deduped[b].severity] ?? 3))
      const keep = deduped[idxs[0]]
      for (const i of idxs.slice(1)) {
        drop.add(i)
        for (const tag of deduped[i].finder.split('+')) {
          if (!keep.finder.split('+').includes(tag)) keep.finder += '+' + tag
        }
      }
    }
    if (drop.size) deduped = deduped.filter((_, i) => !drop.has(i))
    log(`Semantic dedup merged ${drop.size} findings; ${deduped.length} remain`)
  }
}

phase('Verify')
const LENSES = [
  { key: 'correctness', angle: 'CORRECTNESS: Is the claim technically true? Reproduce the alleged failure by tracing the actual code. If the code cannot actually misbehave as claimed, refute.' },
  { key: 'impact', angle: 'SCOPE/IMPACT: Even if technically true, does it matter for this slice? Refute if the spec explicitly accepts the behavior, it is deferred to a later slice without breaking that slice, it is unreachable in practice, or it is a style preference dressed as a bug.' },
]

// Vote tiering: the impact lens is a shallow scope judgment — sonnet/low. The correctness
// lens must trace code, so it scales with the stakes of a wrong verdict.
const voteOpts = (severity, lensKey) => {
  if (lensKey === 'impact') return { model: 'sonnet', effort: 'low' }
  if (severity === 'critical') return { model: 'opus', effort: 'high' }
  if (severity === 'major') return { model: 'opus', effort: 'medium' }
  return { model: 'sonnet', effort: 'medium' }
}

const findingBlock = f => `Finding (from the ${f.finder} finder, severity ${f.severity}):
File: ${f.file}${f.line ? ' line ' + f.line : ''}
Title: ${f.title}
Claim: ${f.claim}`

const judged = await parallel(deduped.map((f, i) => () => (async () => {
  const votes = (await parallel(LENSES.map(l => () =>
    agent(`You are a skeptical verifier. Your job is to REFUTE this code-review finding about the uncommitted slice in the Expensir repo (your working directory). Default to refuted=true if you cannot positively confirm it.
${l.angle}

${findingBlock(f)}

Context: the change is the uncommitted working-tree diff (run \`git status --porcelain\` and \`git diff\`; read new untracked files in full). ${ISSUE} Spec: ${specRefs}. Read the actual code before deciding. Return refuted plus your reasoning.`,
      { label: `refute:${i}:${l.key}`, phase: 'Verify', ...voteOpts(f.severity, l.key), schema: VERDICT_SCHEMA })
      .then(v => (v ? { lens: l.key, refuted: v.refuted, reasoning: v.reasoning } : null))
  ))).filter(Boolean)

  const refutes = votes.filter(v => v.refuted).length
  const confirms = votes.length - refutes

  // Unanimous votes decide; fable adjudicates only splits (and degenerate single-vote refutes).
  let tiebreak = null
  let killed
  if (refutes === 0) killed = false
  else if (confirms === 0 && votes.length >= 2) killed = true
  else {
    tiebreak = await agent(`You are the tiebreak adjudicator for a code-review finding about the uncommitted slice in the Expensir repo (your working directory). Two verifiers disagreed; decide independently. Read the actual code — do not just weigh the arguments below.

${findingBlock(f)}

Prior votes:
${votes.map(v => `- [${v.lens}] ${v.refuted ? 'REFUTED' : 'CONFIRMED'}: ${v.reasoning}`).join('\n')}

Context: the change is the uncommitted working-tree diff (run \`git status --porcelain\` and \`git diff\`; read new untracked files in full). ${ISSUE} Spec: ${specRefs}. Return refuted=true only if you positively establish the finding is wrong, not a real problem, or out of scope for this slice.`,
      { label: `tiebreak:${i}`, phase: 'Verify', model: 'fable', effort: 'high', schema: VERDICT_SCHEMA })
    killed = tiebreak?.refuted === true // a skipped/dead tiebreaker keeps the finding
  }

  return { ...f, votes, tiebreak: tiebreak ? { refuted: tiebreak.refuted, reasoning: tiebreak.reasoning } : null, killed }
})()))

const kept = judged.filter(Boolean)
const confirmed = kept.filter(f => !f.killed)
log(`${confirmed.length}/${deduped.length} findings survived verification`)
return {
  confirmed,
  killed: kept.filter(f => f.killed).map(f => ({
    file: f.file,
    title: f.title,
    severity: f.severity,
    refuteReasons: [
      ...f.votes.filter(v => v.refuted).map(v => `[${v.lens}] ${v.reasoning}`),
      ...(f.tiebreak?.refuted ? [`[tiebreak] ${f.tiebreak.reasoning}`] : []),
    ],
  })),
}
