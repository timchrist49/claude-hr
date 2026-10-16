// Runs a Claude Workflow script with stubbed agent()/parallel()/pipeline() so its control flow can be tested
// without spawning real agents (no tokens). Usage: node workflow_harness.js <script.js> [scenario] [argsJson]
const fs = require('fs');

const file = process.argv[2];
const scenario = process.argv[3] || 'normal';
const argsIn = process.argv[4] ? JSON.parse(process.argv[4]) : {};
const src = fs.readFileSync(file, 'utf8').replace(/^export const meta/m, 'const meta');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const logs = [];
const calls = [];
const prompts = [];
const verifyCalls = {};
const agent = async (prompt, opts = {}) => {
  const label = String(opts.label || '');
  calls.push(label);
  prompts.push({
    label, prompt, agentType: opts.agentType || null, isolation: opts.isolation || null,
    model: opts.model || null, effort: opts.effort || null,
  });
  if (label === 'scope') {
    if (scenario === 'scope-null') return null;
    if (scenario === 'empty') return { base: 'origin/main', files: [], summary: '', empty: true };
    return { base: 'origin/main', files: ['a.py', 'b.py'], summary: 'adds a feature', empty: false };
  }
  if (label.startsWith('find:')) {
    const dim = label.slice(5);
    if (scenario === 'finders-null') return null;
    if (scenario === 'finder-security-throws' && dim === 'security') throw new Error('rate limited');
    if (scenario === 'finder-security-null' && dim === 'security') return null;
    if (dim === 'correctness') {
      return { findings: [
        { file: 'a.py', line: 10, title: 'real bug', detail: 'd', severity: 'high', evidence: 'e' },
        { file: 'a.py', line: 20, title: 'fake bug', detail: 'd', severity: 'medium', evidence: 'e' },
        { file: 'b.py', line: 1, title: 'minor', detail: 'd', severity: 'low', evidence: 'e' },
      ] };
    }
    if (dim === 'security') {
      return { findings: [{ file: 'a.py', line: 10, title: 'same spot, lower severity', detail: 'd', severity: 'low', evidence: 'e' }] };
    }
    return { findings: [] };
  }
  if (label.startsWith('verify:')) {
    if (scenario === 'skeptics-fail') return null;
    // only the first of the two skeptics answers (a partly verified finding)
    if (scenario === 'skeptic-half') {
      const finding = prompt.split(' Claim:')[0]; // the two skeptics of one finding differ only in their lens
      verifyCalls[finding] = (verifyCalls[finding] || 0) + 1;
      if (verifyCalls[finding] > 1) return null;
    }
    return { refuted: /fake bug/.test(prompt), reason: 'r' };
  }
  throw new Error('unexpected agent call: ' + label);
};
const parallel = async thunks => Promise.all(thunks.map(async t => { try { return await t(); } catch (e) { return null; } }));
const pipeline = async (items, ...stages) => Promise.all(items.map(async (item, i) => {
  try { let v = item; for (const s of stages) v = await s(v, item, i); return v; } catch (e) { return null; }
}));

(async () => {
  const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'log', 'phase', 'budget', 'workflow', src);
  const result = await run(argsIn, agent, parallel, pipeline, m => logs.push(m), () => {}, { total: null }, null);
  console.log(JSON.stringify({ result, logs, calls, prompts }));
})().catch(e => { console.error(e.stack || String(e)); process.exit(1); });
