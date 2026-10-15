export const meta = {
  name: 'hr-review-changes',
  description: 'Review the current branch diff across several dimensions, then try to refute every finding before reporting it',
  whenToUse: 'Before merging a non-trivial change. Opt-in only: it spawns roughly 10 to 25 agents.',
  phases: [
    { title: 'Scope', detail: 'find the diff and the changed files' },
    { title: 'Review', detail: 'one finder per dimension, then skeptics try to refute each finding' },
    { title: 'Report', detail: 'dedupe and rank the findings that survived' },
  ],
}

// Optional args: { base: "origin/main", votes: 2, maxPerDimension: 8, dimensions: ["correctness", "security"] }
const A = args || {}
const BASE = A.base || null
const VOTES = Math.max(1, Math.min(5, Number(A.votes) || 2))
const MAX_PER_DIM = Math.max(1, Number(A.maxPerDimension) || 8)

const ALL_DIMENSIONS = [
  { key: 'correctness', lens: 'Look for logic errors, wrong conditions, off-by-one mistakes, unhandled null or empty values, broken invariants and behaviour that differs from what the change intends.' },
  { key: 'security', lens: 'Look for injection, unsafe deserialization, path traversal, missing authorization, secrets in code or logs, SSRF and unsafe handling of untrusted input.' },
  { key: 'robustness', lens: 'Look for missing error handling, resource leaks, race conditions, retries without limits, partial failure leaving bad state, and platform differences.' },
  { key: 'tests', lens: 'Look for changed behaviour that no test exercises, tests that cannot fail, and edge cases the change introduces but does not cover.' },
  { key: 'simplicity', lens: 'Look for duplicated logic, dead code and needless abstraction that this change adds. Only report things a reviewer would ask to remove before merging.' },
]
const wanted = Array.isArray(A.dimensions) && A.dimensions.length ? A.dimensions : null
const DIMENSIONS = wanted ? ALL_DIMENSIONS.filter(d => wanted.includes(d.key)) : ALL_DIMENSIONS

const RANK = { high: 0, medium: 1, low: 2 }
const bySeverity = (a, b) => (RANK[a.severity] ?? 3) - (RANK[b.severity] ?? 3)

const SCOPE = {
  type: 'object',
  properties: {
    base: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    empty: { type: 'boolean' },
  },
  required: ['base', 'files', 'summary', 'empty'],
}
const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          title: { type: 'string' },
          detail: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          evidence: { type: 'string' },
        },
        required: ['file', 'line', 'title', 'detail', 'severity', 'evidence'],
      },
    },
  },
  required: ['findings'],
}
const VERDICT = {
  type: 'object',
  properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['refuted', 'reason'],
}

const SKEPTIC_LENSES = [
  'Trace the code path yourself. Is the failure scenario real?',
  'Does surrounding code, a caller, a type, or an existing test already guard against it?',
  'Try to construct a concrete input or state that triggers it. If you cannot, it is not a finding.',
]

phase('Scope')
const scope = await agent(
  'Determine what to review. Use git to find the change. If the working tree has uncommitted changes, review those together with ' +
    'any commits ahead of the base; otherwise review the current branch against the base. The base is ' +
    (BASE ? BASE : 'the default branch (try origin/main, then origin/master, then main)') +
    '. Return the base you used, the changed files (paths relative to the repo root, excluding lockfiles, generated and vendored ' +
    'files), and a two-sentence summary of what the change does. Set empty to true if there is nothing to review.',
  { phase: 'Scope', label: 'scope', schema: SCOPE, effort: 'low' }
)

if (!scope || scope.empty || !scope.files.length) {
  log('Nothing to review: no changed files were found.')
  return { base: scope ? scope.base : null, files: [], summary: '', confirmed: [], markdown: 'Nothing to review.' }
}
log(`Reviewing ${scope.files.length} file(s) against ${scope.base} across ${DIMENSIONS.length} dimension(s), ${VOTES} skeptic(s) per finding.`)

const finderPrompt = d =>
  `Review the change for ${d.key} problems only. Base: ${scope.base}. Changed files: ${scope.files.join(', ')}. ` +
  `Read the diff yourself (git diff ${scope.base}...HEAD, plus any uncommitted changes) and open the surrounding code when you need it. ${d.lens} ` +
  'Report only issues you can point to in the changed code. For each give the file, the line, a concrete failure scenario in evidence, ' +
  'and a severity. Do not report style or taste. If you find nothing, return an empty list.'

const skepticPrompt = (f, i) =>
  `Try to refute this review finding. File ${f.file} line ${f.line}: ${f.title}. Claim: ${f.detail}. Evidence offered: ${f.evidence}. ` +
  `${SKEPTIC_LENSES[i % SKEPTIC_LENSES.length]} Read the actual code. If you cannot find concrete evidence that the problem is real, set refuted to true.`

phase('Review')
const perDimension = await pipeline(
  DIMENSIONS,
  d => agent(finderPrompt(d), { phase: 'Review', label: `find:${d.key}`, schema: FINDINGS }),
  async (res, d) => {
    const found = ((res && res.findings) || []).slice().sort(bySeverity)
    const kept = found.slice(0, MAX_PER_DIM)
    if (found.length > kept.length) {
      log(`${d.key}: verifying the ${kept.length} most severe of ${found.length} findings; ${found.length - kept.length} lower-severity finding(s) were not checked.`)
    }
    const checked = await parallel(
      kept.map(f => async () => {
        const votes = (
          await parallel(
            Array.from({ length: VOTES }, (_, i) => () =>
              agent(skepticPrompt(f, i), { phase: 'Review', label: `verify:${d.key}`, schema: VERDICT })
            )
          )
        ).filter(Boolean)
        const refutes = votes.filter(v => v.refuted).length
        const killed = votes.length > 0 && refutes >= Math.floor(votes.length / 2) + 1
        return killed ? null : { ...f, dimension: d.key, refutes, answered: votes.length }
      })
    )
    return checked.filter(Boolean)
  }
)

phase('Report')
const survivors = perDimension.filter(Boolean).flat()
const merged = new Map()
for (const f of survivors) {
  const key = `${f.file}:${f.line}`
  const prior = merged.get(key)
  if (!prior || bySeverity(f, prior) < 0) merged.set(key, prior ? { ...f, alsoFoundBy: [...(prior.alsoFoundBy || []), prior.dimension] } : f)
  else prior.alsoFoundBy = [...(prior.alsoFoundBy || []), f.dimension]
}
const confirmed = [...merged.values()].sort(bySeverity)
const unverified = confirmed.filter(f => f.answered === 0).length
log(`${confirmed.length} finding(s) survived verification${unverified ? `, ${unverified} could not be verified (skeptics returned nothing)` : ''}.`)

const markdown = confirmed.length
  ? confirmed
      .map(f => `- [${f.severity}] ${f.file}:${f.line} (${f.dimension}) ${f.title}\n  ${f.detail}\n  Evidence: ${f.evidence}`)
      .join('\n')
  : 'No findings survived verification.'

return { base: scope.base, files: scope.files, summary: scope.summary, confirmed, markdown }
