# claude-hr

A managed Claude Code distribution. `claude-hr` is a drop-in replacement for `claude`: every flag and
subcommand passes straight through, but sessions run through a local [Headroom](https://github.com/headroomlabs-ai/headroom)
proxy, under a resource governor, with vetted plugins, a lean CLAUDE.md baseline, credential guards, and
scheduled, smoke-tested upgrades. One installer sets up a new VM; one command repairs or updates it.
Since 0.2.0 it also gives Claude a verified development loop that costs no tokens: changed files are formatted with the
formatter your repo already uses, the repo's own tests run before a turn can finish, and a failing check goes straight back to
Claude (see [The development loop](#the-development-loop)).

```
claude-hr                                        # interactive, same as `claude`
claude-hr agents --dangerously-skip-permissions  # background agents, routed + governed
claude-hr -p "summarize this repo" --output-format json
claude-hr-ctl doctor                             # health check, with the fix for anything wrong
```

## Install

Needs only `git` and `curl` (Linux/macOS/WSL) or `git` + PowerShell (Windows). The installer adds
[uv](https://docs.astral.sh/uv/) and Claude Code if they are missing, then runs `claude-hr-ctl setup`.
Tested on Windows 11 (PowerShell 5.1) and Ubuntu 22.04 (x86_64, systemd); macOS is covered by unit tests only.

```bash
# Linux / macOS / WSL
curl -fsSL https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.sh | bash
```
```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.ps1 | iex
```

From a checkout instead (also how you test local changes; this installs a copy of the working tree, which cannot
self-update, so re-run the installer from a fresh checkout to upgrade it):

```bash
git clone https://github.com/timchrist49/claude-hr.git && cd claude-hr
./install.sh --local .
```
```powershell
git clone https://github.com/timchrist49/claude-hr.git; cd claude-hr
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Local .   # plain .\install.ps1 only works if your execution policy allows local scripts
```

Useful flags (bash spelling / PowerShell spelling):

- `--profile minimal|standard|process|audit|full|lean-output` / `-Profile`: which plugins to install (default `standard`, see [Plugins](#what-it-does-about-each-concern)). Saved to `~/.claude-hr/config.toml`, so later `setup` / `plugins sync` runs keep it.
- `--no-headroom-install` / `-NoHeadroomInstall`: skip the Headroom install for now. It is large: a download of about 0.5 GB on Windows and about 3.4 GB on Linux x86_64 (it pulls the CUDA build of PyTorch), which is about 2 GB and 6.7 GB on disk. Until you run `claude-hr-ctl setup` without the flag, `claude-hr-ctl doctor` reports `FAIL headroom: not installed` and `claude-hr` runs Claude directly. For a small install (download about 0.2 GB, about 0.8 GB on disk on Linux, without the ML-based features) set `CLAUDE_HR_HEADROOM__FOOTPRINT=lite` before running the installer; setup saves the choice to `~/.claude-hr/config.toml`, and machines under 8 GB RAM get it automatically.
- `--skip-schedule` / `-SkipSchedule`: do not register the scheduled jobs.
- `--shim` / `-Shim`: create a `claude` shim in `~/.claude-hr/bin`. Plain `claude` runs claude-hr only after you put that folder first on your PATH (e.g. `export PATH="$HOME/.claude-hr/bin:$PATH"` in your shell profile; on Windows, in front of your user `Path`). Undo with `claude-hr-ctl shim remove` and delete that PATH entry.
- `--dry-run` / `-DryRun`: previews only the `claude-hr-ctl setup` step (settings, CLAUDE.md, MCP servers, plugins, scheduled jobs). Everything around it still runs: uv and Claude Code are installed if missing, the claude-hr checkout is fetched and moved to the requested ref, the commands are always reinstalled from it, and `--shim` still creates the shim.
- `--no-modify-path` (bash only, or `CLAUDE_HR_NO_MODIFY_PATH=1`): do not add `~/.local/bin` to your shell startup files.
- `--ref vX.Y.Z` / `-Ref vX.Y.Z`: install a release instead of `main`. Self-updates only ever move forward, to the newest `vX.Y.Z` release tag.
- `--repo` / `-Repo` (or `CLAUDE_HR_REPO_URL`): install from a fork. Re-running the installer on a machine that already has claude-hr moves it to the requested ref of that repo (a fresh `origin/main` by default), so it doubles as a manual upgrade.

To pass flags through the one-liners:

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

### First run

Open a new terminal and run `claude-hr-ctl doctor`. If the shell says `command not found`, the tool directory is not on your
PATH yet: run `uv tool update-shell` (use `~/.local/bin/uv` if plain `uv` is not found) and open another terminal.

**Log in once.** Start `claude-hr` and use `/login` (subscription). On a headless VM run `claude setup-token` once and export
the result as `CLAUDE_CODE_OAUTH_TOKEN` from your secret manager. Until a credential exists, claude-hr deliberately starts
Claude *without* Headroom so the login can work (Linux and Windows; on macOS claude-hr cannot see the keychain and routes
immediately, so do the first `/login` with `claude-hr --hr-direct`); quit and run `claude-hr` again and the session is routed. The first routed
launch after a boot may wait up to ~1 minute while the proxy warms up (a scheduled job pre-starts it at login, so you
normally never see this).

On a headless Linux VM run `sudo loginctl enable-linger $USER` once, otherwise the user-level scheduled jobs only run while
someone is logged in.

### Upgrading from 0.1.x

The daily update job moves claude-hr itself to the newest release. 0.2.0 changes defaults that an unattended update never applies on its own (it installs no plugins and passes no `-y`), so after the update run once:

```bash
claude-hr-ctl doctor                                          # lists what is stale, with the exact fix
claude-hr-ctl setup --only settings claude-md plugins         # adds hr-gates, the lean profile and the new permission rules; it only adds
```

The 0.2.0 `standard` profile no longer includes Superpowers, feature-dev or code-simplifier; a machine that already has them keeps them. Uninstall them by name (`claude plugin uninstall superpowers`) to save their tokens, or keep them with `--profile process`. (`plugins sync --prune` removes *every* user-scope plugin outside the profile, including ones you added yourself.) The end-of-turn test gate needs no per-repository setup any more: the first time you start `claude-hr` in a git repository in a normal terminal it offers to turn the gate on (see [Automatic checks](#automatic-checks-the-test-gate)).

## What it does about each concern

| Concern | Mechanism | Where |
|---|---|---|
| **Token usage** | Headroom proxy in its cache-safe `coding` profile (compresses tool output, dedups, defers tool schemas; Headroom's memory and output shaper are off by default, see below); `ENABLE_TOOL_SEARCH` kept on behind the proxy; per-plugin always-on cost audit; native token ledger (`claude-hr-ctl usage`); status line with context %, cost, savings, 5-hour plan usage | `headroom`, `plugins audit`, `usage` |
| **Quality** | No model, effort or thinking setting is written to `settings.json` or the launch environment (Headroom's output shaper, which only adds a "be brief" line to the system prompt and never touches model, effort or thinking, is off by default: `headroom.output_shaper = true` turns it on). CLAUDE.md baseline is ~17 lines; linter enforces budgets, conflicts, dangling references, secrets, stale model scaffolding. Headroom never lossy-compresses file reads. `learn` suggestions are read-only until a human applies them. Subagents that don't need project instructions skip them | `claudemd lint`, `plugins/hr-core` |
| **Parallel agents** | `claude agents` / `--bg` routed through the shared proxy (the Claude daemon freezes the env of whoever starts it; claude-hr detects a daemon started without the proxy and restarts it when idle, otherwise warns). Session limit from RAM and cores (default cap 6; going over it only warns unless `resources.enforce_sessions = "block"`, low free RAM blocks interactive launches). Git-worktree helper and skill. Subagent/tool fan-out caps. Unattended runs wait out 429/529 instead of dying | launcher, `wt`, `daemon` |
| **VM/device resources** | Admission control (session count, free RAM). Linux: systemd cgroup scope per session (`MemoryMax/High`, tasks cap) and the same for Claude's own background spawns via `CLAUDE_CODE_PROCESS_WRAPPER`; needs a systemd user session with D-Bus (`apt install dbus-user-session` on minimal VMs and stock WSL), otherwise sessions run uncapped and `doctor` warns. Windows: Job Object memory cap + kill-on-exit for orphaned MCP servers. macOS: admission control only (CPU priority is lowered only if `resources.low_priority = true`). Disk janitor for what Claude's own sweep misses (`~/.claude/jobs` reached 22 GB here). Monitor with ntfy/webhook alerts | `resources`, `cleanup`, `monitor` |
| **Skills** | `claude-hr-ctl skills` (description size, missing descriptions, name mismatch, listing budget); few skills, and all but one are manual-only (`disable-model-invocation`: not in the skill listing, no always-on tokens; a test enforces it) | `skills` |
| **Code consistency, tests, security** | The end-of-turn gate of `hr-gates` (format changed files with the repo's own formatter, run the trusted fast checks, metadata-only audit trail), a read-only `audit`, scaffolding for CI / Dependabot / issue and PR templates, git-safety confirmations. See [The development loop](#the-development-loop) | `fmt`, `verify`, `audit`, `init-repo`, `trail` |
| **CLAUDE.md** | Managed block that imports `~/.claude/claude-hr/baseline.md`; your own content is never touched; 13 lint rules (`claude-hr-ctl claudemd lint`) | `claudemd` |
| **Plugins** | Declarative profiles applied idempotently (setup only adds; `--prune` also removes user-scope plugins outside the profile). `minimal` = `hr-core` only; `standard` (default, lean: about 160 always-on tokens for our own plugins, measured by `footprint`) = `hr-core` + `hr-gates` + claude-md-management, security-guidance, context7, headroom-snip; `process` = `standard` + superpowers + feature-dev (adds roughly 1.5-2k always-on tokens: only for that flow); `audit` = `standard` + claude-security (deep scans before a release); `full` = `process` + claude-security + skill-creator; `lean-output` = `standard` + the third-party Ponytail plugin (it changes how the model writes code, evaluate first). `security-guidance`'s per-turn Opus review is switched off (`security.stop_review`); its regex layer and commit review stay on. Plugins come only from Anthropic's SHA-pinned catalog or this repo (the one exception is `headroom-snip` from Headroom's own marketplace; turn it off with `headroom.install_plugins = false`); project-scoped candidates (e.g. Vercel) are suggested, not installed globally; redundancy flags | `plugins` |
| **Workflow routing** | Deliberately minimal: 3 read-only subagents with explicit models (`explorer`, `log-triage` on Haiku; `security-reviewer` on Opus; all limited to the Read/Grep/Glob tools), 7 skills of which 6 are manual-only (`hr-plan`, `hr-done`, `hr-gh`, `hr-audit`, `hr-status`, `hr-parallel-worktrees`), 2 opt-in Workflows, no meta-router. See [DESIGN.md](docs/DESIGN.md#10-routing) for why | `plugins/hr-core` |
| **Credentials** | Deny rules for `.env`/ssh/cloud creds; a Bash guard hook that blocks printing secrets into the transcript; `creds doctor` (auth shadowing, plaintext stores) and `creds scan` (secrets already in transcripts, never printed); optional `op run` launch; repo PAT never stored; confirmations (not blocks) for `--no-verify`, force-push of a protected branch, `reset --hard`, `clean -f`; a repository's own test commands run unattended only after you trust that file | `creds`, hook |
| **Updates** | `claude-hr-ctl update` (daily job) updates Claude Code, Headroom, plugins and claude-hr itself. Never downgrades. Claude Code, Headroom and claude-hr itself wait while sessions run (plugin updates do not). Claude Code (native build only) and Headroom get a smoke test and an automatic rollback; Headroom also a 5-day cooldown on new releases (a fresh install takes the newest). claude-hr itself follows the newest `vX.Y.Z` tag with no cooldown, smoke test or rollback: its reinstall runs in the background after the update finishes (on Windows from a one-shot scheduled task) and logs to `~/.claude-hr/logs/self-update.log`; if it did not finish, `doctor` says so and the next update retries. After claude-hr itself was upgraded, the next daily update (or a full `claude-hr-ctl update`) re-applies the managed settings, CLAUDE.md block and plugin set once (never `--prune`, never a Headroom install). Setup turns off Claude's own auto-updater, so if you installed Claude Code with npm/Homebrew/WinGet update it with that tool, or run `claude-hr-ctl config set claude.manage_updates false` and then `claude-hr-ctl setup --only settings` (the flag only reaches `settings.json` when setup runs) | `update` |

## The development loop

Built for one way of working: you plan with `opusplan` and run at Ultracode effort, and you want code that is consistent,
tested, verified and security-checked without paying tokens for the machinery. Everything in this table is deterministic:
it costs **no model tokens** unless something fails or you ask for a review. claude-hr never writes a model, effort or thinking setting, and nothing in the proxy changes them either (the optional output shaper only adds a "be brief" line to the system prompt and is off by default).

| Step | What happens | Cost |
|---|---|---|
| **Format** | At the end of each turn the files git reports as changed in the working tree (Claude's, and any uncommitted or untracked file of yours) are formatted with a formatter the repo already configures (ruff/black, prettier/biome, gofmt, rustfmt; never one it does not set up). A formatter that lives inside the repo (`.venv`, `node_modules/.bin`) and prettier (its configuration can load code) are used only for a repository you trusted (see below) or with `format.repo_tools = "always"`. A tracked file is rewritten only if its committed version was already clean, so you never get a whole-file drift diff. | none; one process per turn |
| **Verify** | The same hook runs the repo's trusted fast checks. A failure goes back to Claude as the reason it cannot stop yet, at most twice per unchanged working tree. Possibly weakened tests (removed asserts, new skip markers) are named in that message. A tree that did not change since the last green run is not checked again. | tokens only when a check fails |
| **Audit trail** | `claude-hr-ctl trail`: one metadata-only record for each turn on which the gate ran (a git repository, not plan mode or a subagent, working tree changed since the last pass): what ran, when, pass or fail. Never code or output. Kept 90 days. | none |
| **Plan, build, prove** | `/hr-core:hr-plan` (a plan file with a verify command per step, your approval, one step at a time), `/hr-core:hr-done` (evidence instead of "it works"), `/hr-core:hr-gh` (issue and PR recipes with the `gh` CLI, no MCP server). For independent steps the opt-in `hr-plan-execute` workflow runs them in parallel git worktrees and merges the passing ones one by one. | manual-only skills: nothing until you call them |
| **Review ladder** | `/hr-core:hr-audit`: the free `claude-hr-ctl audit` first, then the bundled `/code-review`, `/security-review` only for changes at a trust boundary, and the opt-in `hr-review-changes` workflow for a risky release. LLM reviews are advisory; the repo's checks and CI are the gate. | on request |

### Automatic checks (the test gate)

**What it is.** After each turn, claude-hr can run the project's own checks (its linter and its tests) and, if one of them
fails, hand the failure back to Claude to fix before the turn can end. It only uses checks the project already has (for
example `ruff check .` and `pytest -q`) and it costs no tokens unless something fails.

**What you see the first time.** The first time you start `claude-hr` in a folder that is a git repository, in a normal
terminal, it asks once:

```
claude-hr can double-check Claude's work in this folder (optional; asked once per folder).
After each turn it would run this project's own checks and send any failure back to Claude to fix:
    1. ruff check .
    2. pytest -q
Nothing runs unless you say yes now. If you do, they run once, here, to see which pass today.
These checks run code from this folder, so say no if you do not trust it.
Turn it on? [Y/n]
```

Enter means yes. claude-hr then runs each check once, while you watch, and keeps only the ones that pass today (a check that
already fails would only block Claude on a problem it did not cause; those are listed so you can fix them or add them
yourself). The list is saved as `.claude/hr-verify.toml` and kept out of `git status` through `.git/info/exclude` (local to
your clone, never committed by accident). If the folder's agent settings look risky (the same scan as `claude-hr-ctl audit`),
the findings are shown first and the default answer is No.

**How to say no.** Type `n`: you are not asked again for that folder. Ctrl-C or closing the terminal means "not now" and it
asks again next time. To stop the question everywhere: `claude-hr-ctl config set verify.offer off`.

**How to undo it.** `claude-hr-ctl verify disable` turns the gate off for this folder and keeps the file;
`claude-hr-ctl verify enable` turns it on again; `claude-hr-ctl verify status` shows where the folder stands (on, off, or never
offered). To remove it completely, delete `.claude/hr-verify.toml` (and its line in `.git/info/exclude`).

**What it never does.** It never asks in `claude -p`, `claude agents`, `claude mcp ...` or any other subcommand, inside a
Claude Code session, with piped input, a redirected stderr or without a terminal, and no flag or environment variable answers for you. A repository
that brings its own `.claude/hr-verify.toml` (a clone, a pull request) is shown in full, every command of both tiers, with No as
the default; editing the file later asks again. The gate runs the commands you saw and nothing else.

### The manual way

`.claude/hr-verify.toml` is text from that repository (a clone, a PR, a dependency), so it never runs until you have trusted its
exact bytes. The offer above does this for you; to do it by hand:

```bash
claude-hr-ctl verify init --write   # propose checks from what the repo already declares (package.json scripts, pyproject, Cargo, go.mod, Makefile, CI); nothing runs
claude-hr-ctl verify status
claude-hr-ctl verify trust          # in a normal terminal, after reading the file; shows anything `audit` flags first
claude-hr-ctl verify run [--tier full]
```

Editing the file afterwards revokes the trust. There is no `--yes`: `verify trust` and `verify enable` refuse to run inside a Claude Code session
or without a terminal, the bash guard denies an agent that tries it anyway, and `verify run` will not run an untrusted recipe
for an agent. Checks run without a shell, with a scrubbed environment (credential-looking names, cloud/CI/agent prefixes such as `ANTHROPIC_` and `AWS_`, `SSH_AUTH_SOCK` are dropped), a time limit, and the whole process tree is ended on a timeout and when a check exits leaving children running.
`verify trust` shows every check of both tiers in full with terminal and bidi characters escaped, prints the file's SHA-256 and trusts exactly the bytes it showed. A repo without a recipe is simply not gated; formatting still happens.

The rest of the toolbox, all read-only or dry-run by default: `claude-hr-ctl audit` (hooks, MCP servers and skills that run or
fetch code, hidden Unicode and prompt-injection text in CLAUDE.md and READMEs, GitHub workflow risks, masked secrets; `--tools` also
runs gitleaks, pip-audit, npm audit and osv-scanner when installed), `claude-hr-ctl init-repo` (issue forms with an agent-task form,
PR template, CODEOWNERS, Dependabot, `docs/plans` and `docs/decisions`, `REVIEW.md`, `.editorconfig`, `.gitattributes`, a hardened CI skeleton; never overwrites; `--only` picks groups, e.g. leave out `editor` to keep your line endings),
`claude-hr-ctl footprint` (what each plugin costs in always-on context, including what `claude plugin details` does not count),
`claude-hr-ctl fmt`.

Switches: `CLAUDE_HR_GATES=off` in the environment (or `gates.enabled = false`) disables the hook, `format.enabled`,
`verify.gate`, `gates.trail` switch single steps, `verify.offer = "off"` stops the one-time question at launch, `security.profile = "strict"` makes the git confirmations refusals. Everything fails
open: a missing tool, an error or running out of the overall time budget exits quietly and never blocks `claude`. A trusted check that fails, or exceeds its own `timeout_s`, is a failed check and does block (at most `verify.gate_max_blocks` times for the same tree); a check's `timeout_s` is capped by `verify.gate_timeout_s` and by what is left of the hook's 150 s budget, and a check cut short by that shared budget is inconclusive: it never blocks and you get a one-line notice.

## Commands

`claude-hr [--hr-*] <anything claude accepts>` — claude-hr-only switches are stripped before Claude sees them:
`--hr-direct` (skip Headroom), `--hr-wrap` (use `headroom wrap claude`), `--hr-force` (ignore session/RAM
limit), `--hr-no-limits`, `--hr-dry-run` (print the plan and exit), `--hr-verbose`, `--hr-wait N`. Also `CLAUDE_HR_DIRECT=1`.
They change routing and limits only, not the managed settings or plugins. `claude-hr ctl ...` is the same as `claude-hr-ctl ...`.

| `claude-hr-ctl …` | |
|---|---|
| `setup [--only\|--skip headroom settings claude-md mcp plugins schedule] [--profile minimal\|standard\|process\|audit\|full\|lean-output] [--dry-run] [--force] [--prune] [--no-headroom-install]` | install/repair everything; idempotent; keeps your own settings (`--force` overwrites keys you changed; `--prune` also uninstalls user-scope plugins outside the profile; `--profile` is saved) |
| `doctor [--deep] [--json]` / `status` | health with fixes / dashboard |
| `headroom start\|stop\|restart\|status\|logs\|bench\|upgrade` | the shared proxy (`bench` is a compression smoke test; `upgrade` is a plain reinstall of the newest release without cooldown or rollback: prefer `update --only headroom`) |
| `plugins sync\|audit\|update\|suggest\|list` , `skills` | plugin and skill management |
| `claudemd lint\|learn` | CLAUDE.md hygiene / read-only learned-pattern suggestions |
| `usage [--days N] [--by day\|model\|project]` | local token ledger (cache hit rate included) |
| `update [--check] [--only claude headroom plugins self]` | staged upgrades |
| `schedule install\|remove\|status`, `monitor tick\|show` | scheduled jobs / health snapshot |
| `cleanup [--apply]` | reclaim `~/.claude` scratch, MCP logs, oversized daemon log |
| `creds doctor\|scan` | credential hygiene, secrets in transcripts |
| `wt new <name> [--base ref]\|list\|rm <name> [--force] [--delete-branch]` | git worktrees for parallel agents (`.claude/worktrees/<name>` on branch `hr/<name>`; `rm` keeps the branch; `--delete-branch` deletes it only if it is fully merged, otherwise it is kept and the command says so) |
| `workflows list\|install` | copies the opt-in Workflows `hr-review-changes` (parallel review + adversarial verification, typically 10-40 agents and up to about 85 with the defaults; `votes` and `maxPerDimension` raise it) and `hr-plan-execute` (independent plan steps in parallel worktrees, up to ~25 agents, `dryRun` first) into a project's `.claude/workflows/`; they only run when you ask Claude to |
| `fmt [paths] [--changed] [--check]` | no arguments: which formatter this repo configures. With paths or `--changed`: format them (`--check` only reports and exits 1 if something needs formatting) |
| `verify enable\|disable\|status` | the end-of-turn test gate for this folder: `enable` runs the discovered checks once, keeps the ones that pass, asks you, writes `.claude/hr-verify.toml` and trusts it (the same thing the launcher offers once per folder; a person in a terminal only, no `--yes`); `disable` turns it off and stops the offer; `status` shows where the folder stands |
| `verify init [--write] [--force]\|trust\|untrust\|run [--tier fast\|full]` | the manual way: propose the recipe `.claude/hr-verify.toml` from what the repo declares, trust its exact bytes (a person in a terminal only), revoke, run it |
| `audit [path] [--json] [--strict] [--tools]` | read-only, zero-token audit of a repository (agent-config attack surface, hidden Unicode, workflow lint, masked secrets); exit 0 when it ran, 1 with `--strict` on any high or medium finding (an incomplete scan or an unreadable agent file counts), 2 when it could not run |
| `init-repo [path] [--apply] [--only github,docs,editor,dependabot,codeowners,review,ci]` | scaffold the files an agent workflow relies on, at the top level of a git work tree (a plain folder, a bare repository or a subdirectory gets a warning and exit 2 / only the groups that make sense there); a dry run unless `--apply`, creates only files that do not exist, skips the CI skeleton when another workflow already runs tests, never touches git or the network |
| `footprint [--probe] [--strict]` | estimated always-on tokens per enabled plugin and the hidden costs; `--probe` measures one real turn (spends tokens) |
| `trail [--tail N] [--prune]` | the metadata-only audit trail of the end-of-turn gate |
| `daemon status\|restart` | status: is the Claude background service routed through Headroom? restart: stops the service (without checking for running background sessions); the next `claude-hr agents` / `--bg` starts a new one routed through Headroom |
| `shim install\|remove` | create/remove a `claude` shim in `~/.claude-hr/bin` so plain `claude` runs claude-hr. It does not edit PATH: put that folder first on PATH yourself (the command prints the line). On Windows the shim is a real `claude.exe` copy: a `.cmd` file would mangle shell metacharacters and newlines in prompts |
| `config show\|get <key>\|set <key> <value>\|path` | layered config (defaults < `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`) < `~/.claude-hr/config.toml` < `CLAUDE_HR_<SECTION>__<KEY>` env, e.g. `CLAUDE_HR_HEADROOM__PORT=9000`) |
| `uninstall [--purge]` | see [Uninstall](#uninstall) |

## What setup changes (reversible; settings.json and CLAUDE.md are backed up first)

`~/.claude/settings.json` gets a managed baseline merged in (ownership-tracked: your values are never overwritten,
keys you deleted stay deleted, `uninstall` restores originals; a copy is saved to `~/.claude-hr/backups/settings/` before every change).
`~/.claude/CLAUDE.md` gets one marked import block (copy in `backups/claude-md/`) that imports `~/.claude/claude-hr/baseline.md`.
User-scope MCP servers `headroom` and `serena` are registered once (no per-launch cost). Plugins per profile (not backed up;
`uninstall` removes only `hr-core` and `hr-gates`). Scheduled jobs: `warm` (login), `monitor` (15 min), `update` (daily 03:17), `maintain` (weekly).
State, logs, backups and the checkout live in `~/.claude-hr/` (owner-only on Linux/macOS); the `claude-hr` and `headroom-ai`
programs live in uv's tool directory; the scheduled jobs live in your OS scheduler (Linux: `~/.config/systemd/user/claude-hr-*`).
It does **not** put `ANTHROPIC_BASE_URL` in your settings: routing is per launch, so a dead proxy can never break plain `claude`.

Plain `claude` is not stock after setup: it still reads the managed `settings.json` (deny rules, status line, Claude's own
auto-updater off), the CLAUDE.md block, the plugins and the MCP servers. Only the Headroom routing is skipped. To remove all of
that, run `claude-hr-ctl uninstall`.

## Uninstall

```bash
claude-hr-ctl uninstall --purge   # scheduled jobs, managed settings keys, CLAUDE.md block, headroom/serena MCP entries, hr-core and hr-gates plugins + their marketplace, the claude shim file; stops the proxy; --purge also uninstalls Headroom and deletes ~/.claude-hr
uv tool uninstall claude-hr       # the claude-hr / claude-hr-ctl / claude-hr-hook commands themselves
```

Left in place on purpose: the other profile plugins (remove with `claude plugin uninstall <name>`), the
`claude-plugins-official` and `headroom-marketplace` marketplaces, Claude Code and uv, the `~/.local/bin` PATH line that
`install.sh` may have added to your shell startup files (it is uv's standard line, shared with other uv tools; delete it yourself
if you want it gone), any PATH line you added for the shim, and Headroom's downloaded models in the Hugging Face cache
(`~/.cache/huggingface`).

## Known limits (read these)

- **Savings are workload-dependent and often modest.** The only end-to-end figure measured here is 2.5% on a one-word prompt;
  the larger effects come from big repetitive tool output (see [DESIGN.md](docs/DESIGN.md)). Behind any proxy Claude Code turns
  tool search off (174k vs 36k input tokens for a bare prompt with a 200-tool plugin); claude-hr keeps it on, so that is a
  regression avoided, not a saving versus plain `claude`. Measure with `claude-hr-ctl usage` and `headroom savings`; don't
  judge by "tokens saved".
- Behind any custom base URL Claude Code disables **Remote Control (`/rc`)**; use `--hr-direct` for those sessions.
- The proxy holds ~1.2 GB RAM and a cold start is 40–70 s. Machines under 8 GB RAM get a lite install automatically.
- Windows: no Bash sandbox (Claude limitation), no per-agent cgroup equivalent for `claude agents` workers (the
  process-wrapper variable is ignored on Windows).
- Headroom's CCR store keeps plaintext copies of compressed tool output in `~/.claude-hr/headroom` (owner-only on
  Linux/macOS). Treat it like your transcripts. Releases before 0.1.3 registered the `headroom` MCP tool without that
  workspace, so it also wrote `ccr_store.db` and `session_stats.jsonl` to `~/.headroom` (Headroom's default workspace, which also
  holds `memory.db` and your Headroom config if you use Headroom on its own): delete those two files, or the whole folder only if
  you never used Headroom outside claude-hr, then run `claude-hr-ctl setup` to re-register the tool.
- The end-of-turn gate is best-effort: a hook can be disabled (`disableAllHooks`), and it runs while Claude Code waits (on a turn that changed something: up to
  `format.timeout_s` 30 s plus `verify.gate_timeout_s` 120 s plus git overhead, held under one overall budget of 150 s inside the hook's
  170 s timeout). CI is the authoritative gate; `init-repo` scaffolds one.
  The hook calls `claude-hr-hook` by name, so it must be on PATH (`doctor` warns when it is not).
- Pattern checks are not proof: the bash guard, `audit` and the trust-refusal checks catch the common cases and raise the
  cost of the rest (a determined agent can allocate a pty or obfuscate a command). They sit on top of Claude Code's own
  permission prompts; see [SECURITY.md](docs/SECURITY.md).
- [Headroom Desktop](docs/ADDONS.md) (a separate paid tray app) and claude-hr cannot both route Claude: it writes
  `ANTHROPIC_BASE_URL` into `settings.json`, which overrides claude-hr's per-launch routing. `doctor` warns when it sees it.
- `CLAUDE_CODE_PROCESS_WRAPPER`/cgroup caps were verified on WSL2 Ubuntu 22.04 (systemd 249) with `dbus-user-session`
  installed, not on macOS. Without that package the caps are silently skipped (timers still work); `doctor` says so.

## Extending it for your team

- **A team skill or subagent**: fork this repo and install your machines from the fork (`CLAUDE_HR_REPO_URL=<your fork> ./install.sh`,
  or `-Repo <url>` on Windows; machines installed with `--local` never self-update). Add `plugins/hr-core/skills/<name>/SKILL.md`
  (or `agents/<name>.md`), bump the version in all four places together (`src/claude_hr/__init__.py`, `pyproject.toml`, `plugins/hr-core/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`),
  then push a tag named `vX.Y.Z` in the fork (`git tag vX.Y.Z && git push --tags`). Machines installed from the fork pick it up on
  their next `claude-hr-ctl update`. Keep `description` short and specific (at most 300 characters, no ALWAYS/MUST/CRITICAL) and mark every new skill
  `disable-model-invocation: true` (unless you also add its name to `MODEL_INVOKED_SKILLS` in `tests/test_repo_hygiene.py`). Before tagging run `uv run --with pytest pytest -q tests/test_repo_hygiene.py`
  (every skill and agent needs `name` and `description`; versions must agree) and `claude plugin validate plugins/hr-core`
  (validates `plugin.json` only; `claude-hr-ctl skills` audits installed skills, not the checkout).
- **A third-party plugin**: if it is in Anthropic's SHA-pinned catalog, add `<name>@claude-plugins-official` to a profile in
  `src/claude_hr/data/plugins.toml`. For anything else, vendor it into this repo's `.claude-plugin/marketplace.json` with
  `source: {source: url, url: …, sha: <40 chars>}` (a test enforces the full SHA) **and** add `<name>@claude-hr` to a profile in
  `plugins.toml`: being in the marketplace alone installs nothing. The daily `update` only refreshes plugins that are already
  installed, so after the release lands (a self-update) run `claude-hr-ctl plugins sync` (or `setup`) on each machine to install
  the new one; `claude-hr-ctl doctor` lists machines that are missing it.
- **Fleet-wide settings**: `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`); any key in
  `defaults.toml` can be overridden. See which plugins are worth their tokens with `claude-hr-ctl plugins audit` (uses real
  invocation counts from transcripts) and `claude-hr-ctl usage --tools`.

## Documentation

[DESIGN.md](docs/DESIGN.md) architecture, decisions and evidence · [OPERATIONS.md](docs/OPERATIONS.md) day-2 runbook ·
[SECURITY.md](docs/SECURITY.md) credential model · [ADDONS.md](docs/ADDONS.md) what was included, optional, or rejected.

## Develop

```bash
uv run --with pytest pytest -q            # unit tests
uv run --with ruff ruff check src
uv run --with ruff ruff format --check .   # the repo formats itself with the formatter it configures
# fresh-VM install test in a container (needs Docker; downloads Headroom, takes minutes; never run the script directly on your own machine)
docker build -f tests/docker/Dockerfile.linux -t claude-hr-linux-test tests/docker
docker run --rm -v "$PWD:/src:ro" -e SRC_DIR=/src claude-hr-linux-test bash /src/tests/docker/run-linux-e2e.sh
```
```powershell
# Windows: sandboxed full install + 30 checks in %TEMP%\esb3 (~3-5 min; about 0.5 GB download, 2 GB on disk). Needs a logged-in `claude`: makes ONE tiny real
# `claude -p` call. Never writes to your real ~/.claude or ~/.claude-hr.
powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\verify-windows.ps1
# Windows self-update (builds a throwaway git origin with 3 releases, no Headroom download, ~3 min)
powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\verify-windows-selfupdate.ps1
```

## License

[MIT](LICENSE). Third-party components that claude-hr installs or configures (Claude Code, Headroom, Serena and the plugins listed in
[ADDONS.md](docs/ADDONS.md)) are separate works under their own licenses; nothing here changes those terms.
