# Design and decisions

Everything here was either measured on a real machine (Windows 11 + WSL2 Ubuntu 22.04, Claude Code 2.1.300,
Headroom 0.41.0) or taken from a cited source. Where neither holds, it says so.

## Shape of the system

```
                 claude-hr  (python, stdlib only, ~0.15-0.25 s overhead on Windows)
 you ──────────────►  1. classify the command (session | headless | agents | --bg | passthrough)
                      2. admission control: session count + free RAM        (resources.py)
                      3. proxy healthy?  yes ─► env: ANTHROPIC_BASE_URL, ENABLE_TOOL_SEARCH, project headers
                                         no  ─► start it (wait ≤90 s) ─► else FAIL OPEN to plain claude
                      4. exec claude  (POSIX: exec, inside a cgroup scope; Windows: Job Object parent)
                              │
                              ▼                      ┌──────────────────────────────────────────────┐
                           claude ──ANTHROPIC_BASE_URL──►  Headroom proxy  (one, shared, persistent)  │──► api.anthropic.com
                              │                      └──────────────────────────────────────────────┘
                              └─ MCP: headroom (compress/retrieve/stats), serena     (registered once by setup)

 scheduled (systemd timer | launchd | Task Scheduler | cron):
   warm (login) · monitor (15 min) · update (daily) · maintain (weekly)
```

State: everything claude-hr owns is under `~/.claude-hr/` (`repo/`, `state/`, `logs/`, `backups/`, `bin/`, `headroom/`).
Claude's own files are touched only through the ownership-tracked merge (settings), one marked import block
(CLAUDE.md) and the `claude mcp` / `claude plugin` CLIs (never by editing `~/.claude.json`).

## Decisions

### 1. One persistent proxy, not `headroom wrap` per session
`headroom wrap claude` re-registers MCP servers on every start: **5.9 s** to launch `claude --version` through it
(measured), versus ~0.15-0.3 s for the claude-hr launcher (Windows; Python start-up alone is 0.11 s). Its proxy also dies with the session. The proxy itself needs
**40–70 s** to start (imports, model prefetch, embedder warm-up) and holds **~1.2 GB** RSS, so it has to be started
early (login job) and shared. `--hr-wrap` keeps the upstream behaviour available; it is also the only mode where
Headroom's per-project `--code-graph` watcher works, because that flag indexes the proxy's working directory.

### 2. Routing is per launch; `ANTHROPIC_BASE_URL` is never written to settings
A global base URL makes every `claude` depend on the proxy being up. claude-hr sets it in the child environment only,
after a health check, and fails open (`headroom.fail_open`) with a visible warning. The doctor flags a stale global
URL as FAIL.

### 3. `claude agents` / `--bg`: the daemon freezes the environment of its first client
Verified: with no daemon running, `ANTHROPIC_BASE_URL=… claude --bg …` started the daemon, and the worker's requests
appeared at the proxy. The daemon is transient (exits when its last client disconnects) and does not take the
environment of later clients. So claude-hr (a) always starts agent view/`--bg` with the routed environment and (b) detects a
daemon started by something else (by comparing the daemon's starter PID against a ring of PIDs it launched) and, per
`agents.foreign_daemon`, restarts it when idle or warns when it has running sessions. The documented alternative
(gateway URL in `settings.json` `env`) was rejected for the fail-open reason above.

### 4. Tool search must stay on behind a proxy
Claude Code loads every MCP tool schema eagerly when `ANTHROPIC_BASE_URL` is non-first-party. Measured on this machine
(Vercel plugin: 200+ MCP tools): a one-word prompt was **174k input tokens** without `ENABLE_TOOL_SEARCH`, **36k** with it.
The launcher sets it (Headroom's wrapper does the same, issue #746).

### 5. Headroom stays in its default `cache` mode / `coding` profile
Third-party measurements (500-session replay; 25-task benchmark) found `token` mode reduced prompt tokens but raised the
uncached share ~4× (123 vs 14 cache busts) with no cost benefit. `coding` also never lossy-compresses file reads,
which protects answer quality. Expect modest end-to-end savings on interactive coding (measured here: 2.5% on a
trivial prompt; the larger effects come from big repetitive tool output, and tool-schema deferral is a regression avoided
rather than a saving: behind a non-first-party base URL Claude Code turns tool search off, 174k vs 36k input tokens for a bare
prompt with a 200-tool plugin, and claude-hr keeps it on). Judge by
`claude-hr-ctl usage` (cache hit %) and measured spend, not "tokens saved". `headroom.output_holdout = 0.1` makes the
output-savings figure measured instead of estimated.

### 6. Settings: ownership-tracked three-way merge
Leaf-level merge of a baseline into `settings.json` with a record of what was applied and what was there before:
re-runs are no-ops; a baseline change replaces only values claude-hr itself wrote; keys the user edited or deleted are left
alone; `uninstall` restores originals. Lists (permission rules) are additive sets. Tested in `test_settings_merge.py`.
Nothing that changes model behaviour is in the baseline (no model, effort, thinking-token or output-length overrides),
and `disableBypassPermissionsMode` is only set under `security.profile = "strict"` because `--dangerously-skip-permissions`
is an intended workflow here.

### 7. Telemetry flags: only the safe one by default
`DISABLE_ERROR_REPORTING=1` is set. `DISABLE_TELEMETRY` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` also stop
feature-flag fetching (removing `/skill-doctor`, plugin sync and other flagged features), so they are behind
`claude.privacy = "strict"`.

### 8. Plugins
Third-party plugins come only from Anthropic's catalog, where every external entry is pinned to a 40-character SHA, or
from this repo (the single exception is `headroom-snip` from Headroom's own marketplace, installed when
`headroom.install_plugins` is true). `plugins sync` treats a plugin as installed if the same *name* is installed from any marketplace (no
duplicate skills) and never removes anything without `--prune`. Measured always-on cost with `claude plugin details`
(0.1.x `standard`, with Superpowers): ≈ **1.3k tokens**/session (superpowers 703, hr-core 269, feature-dev 194,
claude-md-management 116), plus about 0.8-1k tokens of Superpowers' SessionStart injection that `plugin details` does not
count. 0.2.0's `standard` is lean (`claude-hr-ctl footprint`: our own two plugins about 160 tokens); Superpowers and feature-dev
moved to the opt-in `process` profile.
The Vercel plugin (35 skills, 200+ MCP tools) is *suggested per project*, not installed globally. Headroom's own
`headroom` hooks plugin is **not** installed: it spawns a Python process on every Bash/PowerShell tool call to
ensure a `headroom init` deployment that claude-hr already manages. `headroom-snip` (live savings band) is.

### 9. CLAUDE.md
Anthropic documents that longer files and conflicting rules reduce adherence, the files are re-sent every session and
by every subagent that doesn't set `omitClaudeMd`, and imports do not reduce cost. One study found LLM-generated
context files *lowered* task success slightly while raising cost >20%. Hence: an 17-line baseline, a linter with
measurable thresholds (size, always-loaded budget, emphasis words, derivable content, dangling imports/paths,
contradictions, stale model references, secrets), and `headroom learn` output is a read-only weekly report that a human
applies. The user's own CLAUDE.md content is never edited; claude-hr owns one marked block that imports `claude-hr/baseline.md`.

### 10. Routing
Anthropic's current guidance for Claude 5 models is that heavy routing scaffolding, "ALWAYS use skill X", and
verification-subagent instructions over-trigger and cost more. So: no meta-router skill, no routing table in CLAUDE.md.
`hr-core` ships three read-only subagents with an explicit model and `omitClaudeMd` where appropriate (the model/tool
difference is the reason they exist), one skill per genuinely repeated workflow (7 skills, 6 of them manual-only so they cost nothing until called), and relies on skill/agent
`description` fields for selection. Measure with `claude plugin eval` if you add more.

### 11. Resource governor
- *Admission*: `min((RAM − 4 GB) / 3 GB, cores / 2, 6)` sessions. Exceeding it only warns (`resources.enforce_sessions = "block"` to refuse); below 2 GB free RAM interactive/agent launches are refused, headless runs only warn. A `claude` replacement must never make `claude -p` batches fail with a resource error by default. Anthropic's own advice is 3–5 parallel sessions; memory and rate limits bind before CPU.
- *Hard caps*: Linux `systemd-run --user --scope` (MemoryMax = clamp(40% RAM, 6–16 GB), MemoryHigh = 80% of that, swap off, TasksMax). The launcher probes that the user bus works and falls back to uncapped rather than failing to start Claude (found by the WSL test). `CLAUDE_CODE_PROCESS_WRAPPER` applies the same to the agent-view daemon and its sessions (Linux; the variable is ignored on Windows). Windows: Job Object with a memory cap and kill-on-close so orphaned MCP/dev-server children die; not applied to `agents`/`--bg` (the daemon must outlive the launcher).
- *Not* used: `NODE_OPTIONS=--max-old-space-size` (Claude Code is a native binary; a reported leak ignored it).
- *Disk*: Claude's `cleanupPeriodDays` does not cover `jobs/<id>/tmp`, `mcp-logs-*` or `daemon.log`; `cleanup` and the weekly job do, and never touch a running session's job dir.

### 12. Updates
`DISABLE_AUTOUPDATER=1` (not `DISABLE_UPDATES`, which also blocks `claude install` that the staged updater needs) plus a
daily job. Rules: never downgrade; Claude Code, Headroom and claude-hr itself defer while sessions run (plugin updates do
not); 5-day cooldown on Headroom releases (it ships ~weekly with breaking changes); smoke-test (`/v1/compress` must save
tokens on a synthetic payload; Claude must report the new version and list plugins) and roll back on failure, for Claude
Code and Headroom only. claude-hr itself follows the newest `vX.Y.Z` tag with no cooldown, smoke test or rollback (manual
rollback: docs/OPERATIONS.md). Its reinstall replaces the tool environment the updater runs from, so `update` only queues
it and starts it as the very last act: on Windows from a one-shot Task Scheduler task (uv's `claude-hr-ctl.exe` trampoline
kills any child process we spawn when it exits), on Linux as a transient systemd user unit (the timer's oneshot service
would kill leftovers, hence also `KillMode=process`), on macOS with `AbandonProcessGroup`. The checkout moves before the
reinstall runs, so a failed or killed reinstall is detected by comparing the installed version with the checkout's and
retried by the next update; `doctor` warns about it. Claude release channels are read from
`downloads.claude.ai/claude-code-releases/{stable,latest}`.

### 13. The development loop (0.2.0)
Plan: `docs/plans/2026-10-16-dev-workflow.md`; evidence and rejected options: `docs/ADDONS.md`.
- *One hook, once per turn.* Formatting and checks run in a single `Stop` hook (`hr-gates`), not per edit: a hook spawns a
  Python process (0.11-0.16 s on Windows), so per-tool-call hooks multiply that by hundreds of calls. A per-turn gate sees the
  finished change, which is also what a test run needs. The hook adds no context unless a trusted check failed.
- *Deterministic gates are authoritative, LLM review is advisory.* Hooks are best-effort (a user can disable them), so CI is the
  gate and `init-repo` scaffolds it. No model-judged Stop hooks (they add a model call every turn; `/goal` already covers
  per-task completion), no unattended LLM routines (deferred: they need a stored credential and share the subscription window).
- *Repo-aware, never imposing.* The formatter and the checks come from what the repository already declares. `bun fmt` merged
  2026-10-12 and is canary-only, it does not cover Python, Go or Rust; ruff/prettier/gofmt/rustfmt do.
- *Trust by hash.* A recipe is repository text. The gate runs it only after a person approved its exact bytes, and the approval
  path is hardened against an agent approving itself (SECURITY.md): no `--yes`, `verify run` of an untrusted recipe needs a
  person, the trust command is refused in every spelling the guard recognises (`claude-hr-ctl`, `claude-hr ctl`, `python -m
  claude_hr.ctl`, the trust record file), and each of these has a test.
- *Failure modes.* Every path exits 0 on error. The fingerprint of the working tree plus the recipe's trust state decides
  whether an idle `Stop` does any work; a blocked or errored tree is never remembered, so a fix is verified for real.
- *Parallel work.* Independent plan steps run in worktrees (`hr-plan-execute`, up to 8 at once, merged one by one with the
  checks run before each merge commit); steps that touch the same files are forced sequential.

### 14. The one-time offer of the test gate (0.2.1)
Problem: the gate was safe but nobody had it. Turning it on meant `verify init --write`, reading a TOML file and `verify trust` in
a separate terminal, in every repository; the people it helps most never got that far. Decision: make it effectively default-on
without changing what protects the user, by moving the *question* (not the approval) to the one moment a person is
provably there: starting an interactive session in a normal terminal.
- *The model of trust is untouched.* The recipe is still repository text; running it is still approval of its exact bytes by hash;
  an agent still cannot approve it. The launcher asks only when `human.not_a_person()` says a person is at a terminal (the same
  refusal as `verify trust`: not in a Claude Code shell, not without a terminal on both ends), never in `-p`, `agents`, a
  subcommand, a nested launch or `--hr-dry-run`, and no flag or variable answers for the person.
- *What the person approves is what runs.* The offer shows the discovered commands in plain words; `yes` runs each once while they
  watch, keeps only those that pass today (a gate that blocks Claude on a failure that predates it wastes tokens and trust),
  writes exactly those through the no-symlink writer, hashes the bytes it wrote and trusts that hash. A recipe that ships with
  the repository gets the old full screen (both tiers, escaped, audit preflight, SHA-256) and the default answer No.
- *Default answer follows risk.* Enter is Yes only when the audit preflight (read-only, bounded) finds nothing high or medium,
  or No with the findings printed. EOF and Ctrl-C are "not now" and record nothing; an explicit `n` is remembered per folder in
  `state/verify-offer.json` (a record of an answer, never an approval) and `verify enable|disable` move it either way.
- *Costs nothing when nothing is due.* The launcher imports `gate_offer` only for a plain interactive session; "trusted", "declined" and
  "nothing in this folder to propose from" are decided with a handful of `stat`s and one SHA-256, without importing `verify` or
  `audit` and without a subprocess (about 3 ms, measured), by mirroring three constants of `verify.py` that a test keeps in step.
  Only a folder that has project files but nothing discovery can propose pays for `verify.discover` on each start.
- *Rejected:* an environment variable or flag that auto-answers (it would be the agent's lever too); asking from the Stop hook
  (no terminal there, and the person cannot answer a hook); a modal at first-ever `claude-hr` run (the answer is per folder);
  committing the generated file (it is the user's local choice, so it is kept out of `git status` through `.git/info/exclude`
  instead of the repository's `.gitignore`).

## Failure modes

| Failure | Behaviour |
|---|---|
| Proxy down / cold | Launch waits ≤90 s, then runs plain `claude` with a warning (`fail_open`); monitor restarts the proxy (≤1×/10 min) |
| Proxy crashes on start, or its port is held by another program | Detected immediately (process exit / port-busy), not after 90 s; the next 10 minutes of launches skip the wait entirely (negative cache). `stop()` only ever signals processes whose command line is Headroom, so a dev server on the port is never killed |
| State files locked / corrupt (parallel launches, half-written JSON) | All launch-time bookkeeping is best-effort and retried; a failure never blocks or aborts the `claude` start |
| Update while sessions run (incl. `agents` workers) | Deferred. On Windows replacing the tool env under a running launcher breaks it, so claude-hr's own update and the installers refuse too |
| Headroom not installed | Warning, direct launch |
| Broken `config.toml` | Launcher ignores it with a warning; `claude-hr-ctl` reports the TOML error |
| No user D-Bus (minimal Linux/WSL) | Uncapped launch; doctor/e2e print how to enable caps |
| Daemon started without the route | Restarted if idle, loud warning otherwise |
| Update smoke test fails | Previous version reinstalled, logged to `logs/update.jsonl` |
| claude-hr itself uninstalled | Plain `claude` unaffected (no global base URL); `hr-core` hook commands exit 0 if `claude-hr-hook` is missing; the `hr-gates` Stop hook (exec form, no shell) shows a non-blocking notice each turn until `claude-hr-ctl uninstall` removes the plugin |
| The launcher's gate offer fails, hangs on a hostile repository, is interrupted (Ctrl-C) or hits EOF | Claude starts as before, nothing is recorded or approved; every file it reads from the repository is a size-bounded regular file, the preflight and the checks have time limits, and nothing is written outside `.claude/hr-verify.toml` and `.git/info/exclude` |
| Formatter missing, slow or failing; check times out or cannot start; state file unwritable; any bug in the gate | The turn ends normally; the reason is in `trail` / `logs/crash.log`. Running out of the overall time budget, or a check that cannot start, never blocks; a check that exceeds its own `timeout_s` is a failed check and does block (capped by `verify.gate_max_blocks`) |

## Decisions confirmed with the owner (2026-10-15)
MIT license (switched from all-rights-reserved when the repo went public); default plugin profile `standard` including Superpowers; `claude.privacy = standard` (error reporting off, telemetry left on so flag-gated features work); two saved Workflows, `hr-review-changes` and `hr-plan-execute`, installed per project on request and never run automatically (both validated offline by stubbed drivers, not by spawning agents). 2026-10-16, owner delegated all decisions for 0.2.0 ("I trust your judgement"): lean `standard` profile (Superpowers and feature-dev to `process`), `hr-gates`, manual-only workflow skills, trusted verify recipes, per-turn security review off.

## Not verified
macOS end to end (launchd plists and BSD tools are unit-tested only); Windows `statusLine` shell behaviour in an
interactive TUI (command uses forward slashes and an absolute path); whether Anthropic's Claude 5.x models change
`claude plugin eval` results for the bundled agents; Serena RAM per session.
