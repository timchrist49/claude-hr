---
name: hr-audit
description: Review the change with checks from cheap to costly. Run before merging; go further for risky changes.
disable-model-invocation: true
allowed-tools: Bash(claude-hr-ctl audit *)
---
Start with the free, deterministic step and climb only as far as the risk of the change justifies. After each step, summarise what it found and what it did not check.

0. Run `claude-hr-ctl audit` (agent-config attack surface, workflow lint, secrets) and fix every high finding. Without the helper, look by hand at `.claude/settings*.json` hooks, `.mcp.json`, `.github/workflows` and the diff for secrets, and say that the helper was missing.
1. Run `/code-review` at low or medium effort on the diff. If you cannot start that slash command yourself, ask the user to run it, or read the diff and say that you did.
2. Run `/security-review` only when the diff touches a trust boundary: authentication, input parsing, installers, updaters, hooks, settings merging, workflows or secrets handling. Same rule: ask the user to run it when you cannot.
3. For a risky release, run the opt-in `hr-review-changes` workflow (parallel finders, then skeptics). It spawns many agents: install it with `claude-hr-ctl workflows install` and ask the user first. Never run it on a checkout of an untrusted pull request: the agents read that diff with your permissions.

Finish with a short report: for each step you ran, the findings that stand and the ones you dropped with the reason; for each step you skipped, why. State plainly what was not checked.

LLM reviewers are advisory and can miss things or be wrong. The repo's own checks and CI are the gate, so do not present a clean review as proof that the change is correct.
