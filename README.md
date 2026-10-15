# claude-hr

A managed Claude Code distribution. `claude-hr` is a drop-in replacement for `claude`: every flag and
subcommand passes straight through, but sessions run through a local [Headroom](https://github.com/headroomlabs-ai/headroom)
proxy, under a resource governor, with vetted plugins, a lean CLAUDE.md baseline, credential guards, and
scheduled, smoke-tested upgrades. One installer sets up a new VM; one command repairs or updates it.

```
claude-hr                                        # interactive, same as `claude`
claude-hr agents --dangerously-skip-permissions  # background agents, routed + governed
claude-hr -p "summarize this repo" --output-format json
claude-hr-ctl doctor                             # health check, with the fix for anything wrong
```

## Install

Needs only `git` and `curl` (Linux/macOS/WSL) or `git` + PowerShell (Windows). The installer adds
[uv](https://docs.astral.sh/uv/) and Claude Code if they are missing, then runs `claude-hr-ctl setup`.

```bash
# Linux / macOS / WSL
curl -fsSL https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.sh | bash
# Windows (PowerShell)
irm https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.ps1 | iex
# from a checkout (also how you test local changes)
./install.sh --local .          .\install.ps1 -Local .
```

Useful flags (bash spelling / PowerShell spelling): `--profile minimal|standard|full|lean-output` / `-Profile`,
`--no-headroom-install` / `-NoHeadroomInstall` (skip the ~2 GB download for now), `--skip-schedule` / `-SkipSchedule`,
`--shim` / `-Shim` (make plain `claude` run claude-hr), `--dry-run` / `-DryRun`. To pass them through the one-liners:

```bash
curl -fsSL https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.sh | bash -s -- --profile minimal --no-headroom-install
```
```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.ps1))) -Profile minimal -NoHeadroomInstall
```

The repo is public, so the installers clone `https://github.com/timchrist49/claude-hr.git` anonymously and the scheduled
self-update needs no credentials. **Private fork:** set `CLAUDE_HR_REPO_URL` and export `CLAUDE_HR_GIT_TOKEN` (a fine-grained
PAT, *Contents: read-only*, that repo only) for the first clone. It is sent as a one-shot HTTP header and never written to
disk. For unattended self-updates of a private fork give the VM a read-only **deploy key** and use the SSH URL
(`git@github.com:you/claude-hr.git`), or run `gh auth login`.

Then open a new terminal and run `claude-hr-ctl doctor`. First launch after a boot may wait up to ~1 minute while
the proxy warms up (a scheduled job pre-starts it at login so you normally never see this).

## What it does about each concern

| Concern | Mechanism | Where |
|---|---|---|
| **Token usage** | Headroom proxy in its cache-safe `coding` profile (compresses tool output, dedups, defers tool schemas, optional output shaping); `ENABLE_TOOL_SEARCH` kept on behind the proxy; per-plugin always-on cost audit; native token ledger (`claude-hr-ctl usage`); status line with context %, cost, savings, 5-hour plan usage | `headroom`, `plugins audit`, `usage` |
| **Quality** | Nothing that changes model/effort/thinking is set. CLAUDE.md baseline is ~17 lines; linter enforces budgets, conflicts, dangling references, secrets, stale model scaffolding. Headroom never lossy-compresses file reads. `learn` suggestions are read-only until a human applies them. Subagents that don't need project instructions skip them | `claudemd lint`, `plugins/hr-core` |
| **Parallel agents** | `claude agents` / `--bg` routed through the shared proxy (the Claude daemon freezes the env of whoever starts it; claude-hr detects and fixes a daemon started without the proxy). Session limit from RAM and cores (default cap 6; going over it only warns unless `resources.enforce_sessions = "block"`, low free RAM blocks interactive launches). Git-worktree helper and skill. Subagent/tool fan-out caps. Unattended runs wait out 429/529 instead of dying | launcher, `wt`, `daemon` |
| **VM/device resources** | Admission control (session count, free RAM). Linux: systemd cgroup scope per session (`MemoryMax/High`, tasks cap) and the same for Claude's own background spawns via `CLAUDE_CODE_PROCESS_WRAPPER`. Windows: Job Object memory cap + kill-on-exit for orphaned MCP servers. macOS: priority only. Disk janitor for what Claude's own sweep misses (`~/.claude/jobs` reached 22 GB here). Monitor with ntfy/webhook alerts | `resources`, `cleanup`, `monitor` |
| **Skills** | `skills audit` (description size, missing descriptions, name mismatch, listing budget); few, `disable-model-invocation` where there are side effects | `skills` |
| **CLAUDE.md** | Managed block that imports `~/.claude/claude-hr/baseline.md`; your own content is never touched; 13 lint rules (`claude-hr-ctl claudemd lint`) | `claudemd` |
| **Plugins** | Declarative profiles (`minimal`/`standard`/`full`) reconciled idempotently; plugins come only from Anthropic's SHA-pinned catalog or this repo (the one exception is `headroom-snip` from Headroom's own marketplace; turn it off with `headroom.install_plugins = false`); project-scoped candidates (e.g. Vercel) are suggested, not installed globally; redundancy flags | `plugins` |
| **Workflow routing** | Deliberately minimal: 3 read-only subagents with explicit models (`explorer`, `log-triage` on Haiku; `security-reviewer` on Opus), 3 skills, no meta-router. See [DESIGN.md](docs/DESIGN.md#10-routing) for why | `plugins/hr-core` |
| **Credentials** | Deny rules for `.env`/ssh/cloud creds; a Bash guard hook that blocks printing secrets into the transcript; `creds doctor` (auth shadowing, plaintext stores) and `creds scan` (secrets already in transcripts, never printed); optional `op run` launch; repo PAT never stored | `creds`, hook |
| **Updates** | `claude-hr-ctl update` (daily job): Claude Code, Headroom, plugins, claude-hr itself. Never downgrades, never updates while sessions run, 5-day cooldown on new Headroom releases, smoke test, automatic rollback | `update` |

## Commands

`claude-hr [--hr-*] <anything claude accepts>` — claude-hr-only switches are stripped before Claude sees them:
`--hr-direct` (skip Headroom), `--hr-wrap` (use `headroom wrap claude`), `--hr-force` (ignore session/RAM
limit), `--hr-no-limits`, `--hr-dry-run`, `--hr-verbose`, `--hr-wait N`. Also `CLAUDE_HR_DIRECT=1`.
`claude-hr ctl ...` is the same as `claude-hr-ctl ...`.

| `claude-hr-ctl …` | |
|---|---|
| `setup [--only/--skip step] [--profile p] [--dry-run] [--force]` | install/repair everything; idempotent; keeps your own settings |
| `doctor [--deep] [--json]` / `status` | health with fixes / dashboard |
| `headroom start\|stop\|restart\|status\|logs\|bench\|upgrade` | the shared proxy |
| `plugins sync\|audit\|update\|suggest\|list` , `skills` | plugin and skill management |
| `claudemd lint\|learn` | CLAUDE.md hygiene / read-only learned-pattern suggestions |
| `usage [--days N] [--by day\|model\|project]` | local token ledger (cache hit rate included) |
| `update [--check] [--only claude headroom plugins self]` | staged upgrades |
| `schedule install\|remove\|status`, `monitor tick\|show` | scheduled jobs / health snapshot |
| `cleanup [--apply]` | reclaim `~/.claude` scratch, MCP logs, oversized daemon log |
| `creds doctor\|scan` | credential hygiene, secrets in transcripts |
| `wt new\|list\|rm <name>` | git worktrees for parallel agents |
| `workflows list\|install` | copies the opt-in `hr-review-changes` Workflow (parallel review + adversarial verification, ~10-25 agents) into a project's `.claude/workflows/`; it only runs when you ask Claude to run it |
| `daemon status\|restart` | is the Claude background service routed through Headroom? |
| `shim install\|remove` | make plain `claude` run claude-hr (on Windows a real `claude.exe` copy: a `.cmd` file would mangle shell metacharacters and newlines in prompts) |
| `config show\|get\|set\|path` | layered config (defaults < `/etc` < `~/.claude-hr/config.toml` < `CLAUDE_HR_*` env) |
| `uninstall [--purge]` | revert managed config only |

## What setup changes (all reversible, all backed up)

`~/.claude/settings.json` gets a managed baseline merged in (ownership-tracked: your values are never overwritten,
removed keys you deleted stay deleted, `uninstall` restores originals — backups in `~/.claude-hr/backups`).
`~/.claude/CLAUDE.md` gets one marked import block. User-scope MCP servers `headroom` and `serena` are registered
once (no per-launch cost). Plugins per profile. Scheduled jobs: `warm` (login), `monitor` (15 min), `update`
(daily 03:17), `maintain` (weekly). Everything claude-hr owns lives in `~/.claude-hr/`. It does **not** put
`ANTHROPIC_BASE_URL` in your settings: routing is per launch, so a dead proxy can never break plain `claude`.

## Known limits (read these)

- **Savings are workload-dependent and often modest.** Independent tests report single-digit % of spend in
  cache-safe mode; the large wins are tool-schema deferral and big repetitive tool output. Measure with
  `claude-hr-ctl usage` and `headroom savings`; don't judge by "tokens saved".
- Behind any custom base URL Claude Code disables **Remote Control (`/rc`)**; use `--hr-direct` for those sessions.
- The proxy holds ~1.2 GB RAM and a cold start is 40–70 s. Machines under 8 GB RAM get a lite install automatically.
- Windows: no Bash sandbox (Claude limitation), no per-agent cgroup equivalent for `claude agents` workers (the
  process-wrapper variable is ignored on Windows).
- Headroom's CCR store keeps plaintext copies of compressed tool output under `~/.claude-hr/headroom`.
- `CLAUDE_CODE_PROCESS_WRAPPER`/cgroup caps were verified on WSL2 Ubuntu 22.04 (systemd 249), not on macOS.

## Extending it for your team

- **A team skill or subagent**: add `plugins/hr-core/skills/<name>/SKILL.md` (or `agents/<name>.md`), bump the version in
  `plugin.json` *and* `.claude-plugin/marketplace.json`, tag a release. Machines pick it up on their next `claude-hr-ctl update`.
  Keep `description` short and specific, mark side-effecting workflows `disable-model-invocation: true`, and run
  `claude-hr-ctl skills` and `claude plugin validate plugins/hr-core` before tagging.
- **A third-party plugin**: add it to a profile in `src/claude_hr/data/plugins.toml` only if it is in Anthropic's SHA-pinned
  catalog; for anything else, vendor it into this repo's marketplace with `source: {source: url, url: …, sha: <40 chars>}`.
- **Fleet-wide settings**: `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`); any key in
  `defaults.toml` can be overridden. See which plugins are worth their tokens with `claude-hr-ctl plugins audit` (uses real
  invocation counts from transcripts) and `claude-hr-ctl usage --tools`.

## Documentation

[DESIGN.md](docs/DESIGN.md) architecture, decisions and evidence · [OPERATIONS.md](docs/OPERATIONS.md) day-2 runbook ·
[SECURITY.md](docs/SECURITY.md) credential model · [ADDONS.md](docs/ADDONS.md) what was included, optional, or rejected.

## Develop

```bash
uv run --with pytest pytest -q            # unit tests (also run on Linux/macOS/Windows in CI)
uv run --with ruff ruff check src
tests/docker/run-linux-e2e.sh             # fresh-VM install test (run inside tests/docker/Dockerfile.linux)
powershell -File tests\windows\verify-windows.ps1   # Windows: full sandboxed install + 30 checks (no changes to your real profile)
```
