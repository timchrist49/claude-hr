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
trivial prompt; the large effects are tool-schema deferral and big repetitive tool output). Judge by
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
duplicate skills) and never removes anything without `--prune`. Measured always-on cost with `claude plugin details`:
`standard` profile ≈ **1.3k tokens**/session (superpowers 703, hr-core 269, feature-dev 194, claude-md-management 116).
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
difference is the reason they exist), one skill per genuinely repeated workflow, and relies on skill/agent
`description` fields for selection. Measure with `claude plugin eval` if you add more.

### 11. Resource governor
- *Admission*: `min((RAM − 4 GB) / 3 GB, cores / 2, 6)` sessions. Exceeding it only warns (`resources.enforce_sessions = "block"` to refuse); below 2 GB free RAM interactive/agent launches are refused, headless runs only warn. A `claude` replacement must never make `claude -p` batches fail with a resource error by default. Anthropic's own advice is 3–5 parallel sessions; memory and rate limits bind before CPU.
- *Hard caps*: Linux `systemd-run --user --scope` (MemoryMax = clamp(40% RAM, 6–16 GB), MemoryHigh = 80% of that, swap off, TasksMax). The launcher probes that the user bus works and falls back to uncapped rather than failing to start Claude (found by the WSL test). `CLAUDE_CODE_PROCESS_WRAPPER` applies the same to the agent-view daemon and its sessions (Linux; the variable is ignored on Windows). Windows: Job Object with a memory cap and kill-on-close so orphaned MCP/dev-server children die; not applied to `agents`/`--bg` (the daemon must outlive the launcher).
- *Not* used: `NODE_OPTIONS=--max-old-space-size` (Claude Code is a native binary; a reported leak ignored it).
- *Disk*: Claude's `cleanupPeriodDays` does not cover `jobs/<id>/tmp`, `mcp-logs-*` or `daemon.log`; `cleanup` and the weekly job do, and never touch a running session's job dir.

### 12. Updates
`DISABLE_AUTOUPDATER=1` (not `DISABLE_UPDATES`, which also blocks `claude install` that the staged updater needs) plus a
daily job. Rules: never downgrade, defer while sessions run, 5-day cooldown on Headroom releases (it ships ~weekly with
breaking changes), smoke-test (`/v1/compress` must save tokens on a synthetic payload; Claude must report the new
version and list plugins), roll back on failure. Claude release channels are read from
`downloads.claude.ai/claude-code-releases/{stable,latest}`.

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
| claude-hr itself uninstalled | Plain `claude` unaffected (no global base URL); hook commands exit 0 if `claude-hr-hook` is missing |

## Decisions confirmed with the owner (2026-10-15)
Proprietary, all-rights-reserved license (kept as chosen before the repo went public; see LICENSE); default plugin profile `standard` including Superpowers; `claude.privacy = standard` (error reporting off, telemetry left on so flag-gated features work); one saved Workflow, `hr-review-changes`, installed per project on request and never run automatically (validated by a stubbed harness, `tests/workflow_harness.js`, not by spawning agents).

## Not verified
macOS end to end (launchd plists and BSD tools are unit-tested only); Windows `statusLine` shell behaviour in an
interactive TUI (command uses forward slashes and an absolute path); whether Anthropic's Claude 5.x models change
`claude plugin eval` results for the bundled agents; Serena RAM per session.
