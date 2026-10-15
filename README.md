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

- `--profile minimal|standard|full|lean-output` / `-Profile`: which plugins to install (default `standard`, see [Plugins](#what-it-does-about-each-concern)). Saved to `~/.claude-hr/config.toml`, so later `setup` / `plugins sync` runs keep it.
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

## What it does about each concern

| Concern | Mechanism | Where |
|---|---|---|
| **Token usage** | Headroom proxy in its cache-safe `coding` profile (compresses tool output, dedups, defers tool schemas, output shaping on by default: `headroom.output_shaper = false` turns it off); `ENABLE_TOOL_SEARCH` kept on behind the proxy; per-plugin always-on cost audit; native token ledger (`claude-hr-ctl usage`); status line with context %, cost, savings, 5-hour plan usage | `headroom`, `plugins audit`, `usage` |
| **Quality** | No model, effort or thinking setting is written to `settings.json` or the launch environment (Headroom's output shaper, on by default, steers output verbosity and effort inside the proxy; `headroom.output_shaper = false` disables it). CLAUDE.md baseline is ~17 lines; linter enforces budgets, conflicts, dangling references, secrets, stale model scaffolding. Headroom never lossy-compresses file reads. `learn` suggestions are read-only until a human applies them. Subagents that don't need project instructions skip them | `claudemd lint`, `plugins/hr-core` |
| **Parallel agents** | `claude agents` / `--bg` routed through the shared proxy (the Claude daemon freezes the env of whoever starts it; claude-hr detects a daemon started without the proxy and restarts it when idle, otherwise warns). Session limit from RAM and cores (default cap 6; going over it only warns unless `resources.enforce_sessions = "block"`, low free RAM blocks interactive launches). Git-worktree helper and skill. Subagent/tool fan-out caps. Unattended runs wait out 429/529 instead of dying | launcher, `wt`, `daemon` |
| **VM/device resources** | Admission control (session count, free RAM). Linux: systemd cgroup scope per session (`MemoryMax/High`, tasks cap) and the same for Claude's own background spawns via `CLAUDE_CODE_PROCESS_WRAPPER`; needs a systemd user session with D-Bus (`apt install dbus-user-session` on minimal VMs and stock WSL), otherwise sessions run uncapped and `doctor` warns. Windows: Job Object memory cap + kill-on-exit for orphaned MCP servers. macOS: admission control only (CPU priority is lowered only if `resources.low_priority = true`). Disk janitor for what Claude's own sweep misses (`~/.claude/jobs` reached 22 GB here). Monitor with ntfy/webhook alerts | `resources`, `cleanup`, `monitor` |
| **Skills** | `claude-hr-ctl skills` (description size, missing descriptions, name mismatch, listing budget); few skills, `disable-model-invocation` on those with side effects | `skills` |
| **CLAUDE.md** | Managed block that imports `~/.claude/claude-hr/baseline.md`; your own content is never touched; 13 lint rules (`claude-hr-ctl claudemd lint`) | `claudemd` |
| **Plugins** | Declarative profiles applied idempotently (setup only adds; `--prune` also removes user-scope plugins outside the profile). `minimal` = `hr-core` only; `standard` (default) = `hr-core` + claude-md-management, feature-dev, security-guidance, code-simplifier, context7, superpowers, headroom-snip; `full` = `standard` + skill-creator; `lean-output` = `standard` + the third-party Ponytail plugin (it changes how the model writes code, evaluate first). Plugins come only from Anthropic's SHA-pinned catalog or this repo (the one exception is `headroom-snip` from Headroom's own marketplace; turn it off with `headroom.install_plugins = false`); project-scoped candidates (e.g. Vercel) are suggested, not installed globally; redundancy flags | `plugins` |
| **Workflow routing** | Deliberately minimal: 3 read-only subagents with explicit models (`explorer`, `log-triage` on Haiku; `security-reviewer` on Opus; all limited to the Read/Grep/Glob tools), 3 skills, no meta-router. See [DESIGN.md](docs/DESIGN.md#10-routing) for why | `plugins/hr-core` |
| **Credentials** | Deny rules for `.env`/ssh/cloud creds; a Bash guard hook that blocks printing secrets into the transcript; `creds doctor` (auth shadowing, plaintext stores) and `creds scan` (secrets already in transcripts, never printed); optional `op run` launch; repo PAT never stored | `creds`, hook |
| **Updates** | `claude-hr-ctl update` (daily job) updates Claude Code, Headroom, plugins and claude-hr itself. Never downgrades. Claude Code, Headroom and claude-hr itself wait while sessions run (plugin updates do not). Claude Code (native build only) and Headroom get a smoke test and an automatic rollback; Headroom also a 5-day cooldown on new releases (a fresh install takes the newest). claude-hr itself follows the newest `vX.Y.Z` tag with no cooldown, smoke test or rollback: its reinstall runs in the background after the update finishes (on Windows from a one-shot scheduled task) and logs to `~/.claude-hr/logs/self-update.log`; if it did not finish, `doctor` says so and the next update retries. Setup turns off Claude's own auto-updater, so if you installed Claude Code with npm/Homebrew/WinGet update it with that tool, or run `claude-hr-ctl config set claude.manage_updates false` and then `claude-hr-ctl setup --only settings` (the flag only reaches `settings.json` when setup runs) | `update` |

## Commands

`claude-hr [--hr-*] <anything claude accepts>` — claude-hr-only switches are stripped before Claude sees them:
`--hr-direct` (skip Headroom), `--hr-wrap` (use `headroom wrap claude`), `--hr-force` (ignore session/RAM
limit), `--hr-no-limits`, `--hr-dry-run` (print the plan and exit), `--hr-verbose`, `--hr-wait N`. Also `CLAUDE_HR_DIRECT=1`.
They change routing and limits only, not the managed settings or plugins. `claude-hr ctl ...` is the same as `claude-hr-ctl ...`.

| `claude-hr-ctl …` | |
|---|---|
| `setup [--only\|--skip headroom settings claude-md mcp plugins schedule] [--profile minimal\|standard\|full\|lean-output] [--dry-run] [--force] [--prune] [--no-headroom-install]` | install/repair everything; idempotent; keeps your own settings (`--force` overwrites keys you changed; `--prune` also uninstalls user-scope plugins outside the profile; `--profile` is saved) |
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
| `workflows list\|install` | copies the opt-in `hr-review-changes` Workflow (parallel review + adversarial verification, ~10-25 agents) into a project's `.claude/workflows/`; it only runs when you ask Claude to run it |
| `daemon status\|restart` | status: is the Claude background service routed through Headroom? restart: stops the service (without checking for running background sessions); the next `claude-hr agents` / `--bg` starts a new one routed through Headroom |
| `shim install\|remove` | create/remove a `claude` shim in `~/.claude-hr/bin` so plain `claude` runs claude-hr. It does not edit PATH: put that folder first on PATH yourself (the command prints the line). On Windows the shim is a real `claude.exe` copy: a `.cmd` file would mangle shell metacharacters and newlines in prompts |
| `config show\|get <key>\|set <key> <value>\|path` | layered config (defaults < `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`) < `~/.claude-hr/config.toml` < `CLAUDE_HR_<SECTION>__<KEY>` env, e.g. `CLAUDE_HR_HEADROOM__PORT=9000`) |
| `uninstall [--purge]` | see [Uninstall](#uninstall) |

## What setup changes (reversible; settings.json and CLAUDE.md are backed up first)

`~/.claude/settings.json` gets a managed baseline merged in (ownership-tracked: your values are never overwritten,
keys you deleted stay deleted, `uninstall` restores originals; a copy is saved to `~/.claude-hr/backups/settings/` before every change).
`~/.claude/CLAUDE.md` gets one marked import block (copy in `backups/claude-md/`) that imports `~/.claude/claude-hr/baseline.md`.
User-scope MCP servers `headroom` and `serena` are registered once (no per-launch cost). Plugins per profile (not backed up;
`uninstall` removes only `hr-core`). Scheduled jobs: `warm` (login), `monitor` (15 min), `update` (daily 03:17), `maintain` (weekly).
State, logs, backups and the checkout live in `~/.claude-hr/` (owner-only on Linux/macOS); the `claude-hr` and `headroom-ai`
programs live in uv's tool directory; the scheduled jobs live in your OS scheduler (Linux: `~/.config/systemd/user/claude-hr-*`).
It does **not** put `ANTHROPIC_BASE_URL` in your settings: routing is per launch, so a dead proxy can never break plain `claude`.

Plain `claude` is not stock after setup: it still reads the managed `settings.json` (deny rules, status line, Claude's own
auto-updater off), the CLAUDE.md block, the plugins and the MCP servers. Only the Headroom routing is skipped. To remove all of
that, run `claude-hr-ctl uninstall`.

## Uninstall

```bash
claude-hr-ctl uninstall --purge   # scheduled jobs, managed settings keys, CLAUDE.md block, headroom/serena MCP entries, hr-core plugin + its marketplace, the claude shim file; stops the proxy; --purge also uninstalls Headroom and deletes ~/.claude-hr
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
- `CLAUDE_CODE_PROCESS_WRAPPER`/cgroup caps were verified on WSL2 Ubuntu 22.04 (systemd 249) with `dbus-user-session`
  installed, not on macOS. Without that package the caps are silently skipped (timers still work); `doctor` says so.

## Extending it for your team

- **A team skill or subagent**: fork this repo and install your machines from the fork (`CLAUDE_HR_REPO_URL=<your fork> ./install.sh`,
  or `-Repo <url>` on Windows; machines installed with `--local` never self-update). Add `plugins/hr-core/skills/<name>/SKILL.md`
  (or `agents/<name>.md`), bump the version in `plugins/hr-core/.claude-plugin/plugin.json` *and* `.claude-plugin/marketplace.json`,
  then push a tag named `vX.Y.Z` in the fork (`git tag vX.Y.Z && git push --tags`). Machines installed from the fork pick it up on
  their next `claude-hr-ctl update`. Keep `description` short and specific (under ~400 characters) and mark side-effecting
  workflows `disable-model-invocation: true`. Before tagging run `uv run --with pytest pytest -q tests/test_repo_hygiene.py`
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
