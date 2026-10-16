# Changelog

## 0.2.1
The end-of-turn test gate is now offered, once per folder, instead of waiting for you to find three commands. The model of trust
is unchanged: the checks still run only after a person approved the exact file, and an agent still cannot approve it.
- **One-time offer at launch.** The first time you start an interactive `claude-hr` session in a git repository in a normal
  terminal, it asks `Turn it on? [Y/n]` in plain words and lists the commands it found. Yes runs each check once while you watch,
  keeps only the ones that pass today, writes `.claude/hr-verify.toml`, hides it from `git status` (`.git/info/exclude`, local) and
  trusts the exact bytes it wrote. No is remembered for that folder; Ctrl-C is "not now". The default answer is No, with the findings
  printed first, when the audit preflight finds anything high or medium. A recipe that ships with a repository is shown in full
  (both tiers, escaped, SHA-256) and asked about with No as the default. Never asked in `-p`, `agents`, subcommands, a Claude Code
  shell, with piped input, in a home directory or a bare repository; nothing answers for you (no flag, no variable). New config key
  `verify.offer` (`"ask"`, or `"off"`).
- **New commands:** `claude-hr-ctl verify enable` (the same thing on request, a person at a terminal only, no `--yes`) and
  `verify disable` (untrust, remember the no, keep the file). `verify status` says when a folder was declined or never offered;
  `verify trust` by hand also stops the offer for that folder. The bash guard denies an agent that types `verify enable` /
  `disable` / `trust` / `untrust`; the settings baseline asks before `verify trust` and `verify enable`.
- **Cost:** about 3 ms on an interactive start in a folder that is already decided, and nothing on any other launch (`-p`, agents,
  subcommands, dry runs do not even import the module). The launcher stays fail-open: any error in the offer starts Claude.
- The manual way (`verify init --write`, `verify trust`) still works and is documented as such.
- **The offer waits for Claude Code's own trust.** It is made only in a folder Claude Code itself has already been told to trust
  (`hasTrustDialogAccepted` in its state file), so a freshly cloned repository is offered nothing on the first launch; a person who
  just pressed Enter can no longer run an untrusted clone's commands. `verify enable` works anywhere.
- **Two Headroom defaults changed, from measurements on a real install (2026-10-17):**
  - `headroom.memory` is now off. In `proxy_mode = "cache"` it injected 5 tools (about 2,460 tokens) into every request and ran a
    lookup (0.3-1.8 s) whose result was thrown away; no memory had ever been saved and no benefit is measured. Claude Code's own
    CLAUDE.md and MEMORY.md do the job. Set `headroom.memory = true` to bring it back.
  - `headroom.output_shaper` is now off. In Headroom 0.41.0 it only appends a short "be brief, do not restate code" instruction to
    the system prompt (it never changed model, effort or thinking; the README had said it steers effort, which was wrong), and its
    benefit was never measured on real traffic. Set `headroom.output_shaper = true` to bring it back. Run `claude-hr-ctl headroom
    restart` once for either change to reach the shared proxy.
  Together with disabling unused plugins this took a fresh session on one machine from 43,791 to 30,588 input tokens (-30%).
- **Found by an adversarial review of the offer, fixed before release:**
  - A stray Enter could answer the question: pending console input is now discarded right before it, the wait ends after five
    minutes as "not now", two unclear answers are "not now" (nothing recorded), and on Windows `NUL` no longer counts as a terminal.
  - The screen now shows what a task-runner check will run (`npm run` script text, Makefile or justfile recipe lines), says that
    the project's own scripts and the formatter's repo tools are part of the yes, names the repository folder when it differs from
    where Claude started, and the default turns to No when a shown script looks like fetching or sending data, decoding and
    running, reading credentials or deleting your home.
  - Only the repository's top folder counts as trusted by Claude Code (not a launch subfolder or a trusted parent).
  - The Stop hook ignores a state folder that lies inside the repository or the working folder, so a repository can no longer
    ship its own trust record through project settings (`CLAUDE_HR_HOME`). This closes a gap that existed in 0.2.0.
  - The bash guard's verify/trust patterns can no longer be made to time out with a padded command (bounded gaps, first and
    last 16 KiB, about 0.05 s per command; the secret and git patterns are unchanged and still unbounded).
  - Recipes are kept out of git in linked worktrees and submodules too; a check that edits tracked files while it is probed is
    left out; a folder turned on and then emptied is not asked again; a folder with nothing to propose is remembered until a
    project file changes (no repeated discovery cost); a broken `config.toml` no longer drops `verify.offer = "off"`; discovery
    proposes `python3` where only that exists; the end of `verify enable` says when the `hr-gates` plugin is not enabled.
  - `doctor` warns when the running Headroom proxy still has settings older than the current ones (`headroom restart` applies
    them; it now says how many sessions are running).
  - Known limits, documented in SECURITY.md: the approval covers the recipe's bytes, not the scripts it runs; a process that fakes
    a terminal can still answer the question (as with `verify trust` in 0.2.0); the guard's secret and git patterns can be padded
    past the hook time limit (fails open); a check that starts failing for reasons outside Claude's edits blocks twice per turn.
- **Second review round (the fixes themselves), fixed before release:**
  - A hostile `rm -r -r ...` line in a script could freeze the launcher for minutes before the question appeared: the
    suspicious-script scan is now linear, bounded (2,000 characters per line, 0.25 s per scan, over that the default is No).
  - The Makefile view shows the last rule that defines the target and its prerequisites, and says (default No) when it cannot
    show what make would run (defined twice, `include`, unreadable prerequisite, cut short). A script the screen cannot read
    (`--prefix`, `-C`, workspaces) says so and defaults to No. Ordinary scripts no longer trip the pattern list (`.env.example`,
    `localhost`, `jest tests/base64.test.js`, comments).
  - "Later" instead of a permanent no: Enter on a default-No screen asks again after 7 days, a yes where nothing passes yet
    (a clone before `npm install`) after 3 days, an unanswered question after 1 day; `n`/`no`/`nope`/`no thanks` still decline for good.
  - The edit probe compares file content, so a check that only touches a file is not "editing tracked files".
  - The hooks write nothing into a state folder the repository chose (config read, crash log, status line cache), never write
    through a link, and `doctor` / `verify status` / the offer say when the gate cannot run for that reason.
- Tests no longer depend on the machine: the sandbox hides git's global and system configuration and `XDG_CONFIG_HOME`, the
  CLAUDE.md tests read only files inside their own folder, the Headroom footprint tests set the footprint they need, and the
  "never asked" tests fail when an unscripted question is asked (they used to pass vacuously because the launcher swallowed
  the test's own error).

## 0.2.0 (2026-10-16)
A verified development loop with no always-on token cost, for sessions run at Ultracode effort on `opusplan` (claude-hr still sets
no model, effort or thinking setting; Headroom's own output shaper stays a separate switch). Plan: `docs/plans/2026-10-16-dev-workflow.md`; reasons: `docs/DESIGN.md` section 13.
- **New plugin `hr-gates`** (in the `standard` profile): one `Stop` hook, `claude-hr-hook stop`, once per turn. It formats the files
  git reports as changed in the working tree with the formatter the repo already configures (ruff/black, prettier/biome, gofmt, rustfmt), runs the repo's trusted
  fast checks and returns a failure to Claude, and appends a metadata-only audit trail. It adds no context unless a trusted check
  failed, skips plan mode and subagents, and fails open on every error.
- **New commands:** `fmt`, `verify init|status|trust|untrust|run`, `audit`, `init-repo`, `footprint`, `trail`. `verify` runs only a
  recipe (`.claude/hr-verify.toml`) whose exact bytes a person trusted; `audit` is a read-only, zero-token audit of a repository's
  agent surface, workflows and secrets; `init-repo` scaffolds CI (job `verify`), Dependabot, issue forms, a PR template,
  CODEOWNERS, `docs/plans`, `docs/decisions`, `REVIEW.md`, `.editorconfig` and `.gitattributes` (dry run by default, never overwrites); `footprint` estimates each
  plugin's always-on tokens including the costs `claude plugin details` does not count.
- **New skills in `hr-core`, all manual-only (no always-on tokens):** `hr-plan`, `hr-done`, `hr-gh` (the `gh` CLI, no MCP server),
  `hr-audit`. New opt-in workflow `hr-plan-execute` (independent plan steps in parallel git worktrees, merged one by one).
- **Leaner `standard` profile.** It now holds `hr-core`, `hr-gates`, claude-md-management, security-guidance, context7 and
  headroom-snip. Superpowers and feature-dev moved to the new `process` profile, code-simplifier is listed as redundant with the
  bundled `/simplify`; new profiles `process` and `audit`; `full` extends `process`. Existing machines keep what they have until you
  run `claude-hr-ctl plugins sync --prune`.
- **security-guidance's per-turn Opus review is off by default** (`env.ENABLE_STOP_REVIEW=0` in the managed baseline; its regex layer
  and commit review stay on). `security.stop_review = "plugin-default"` restores it. Audit on demand with `/security-review` or
  `claude-hr-ctl audit`.
- **Git safety:** the bash guard asks before `--no-verify`, force-push of a protected branch, `reset --hard`, `clean -f` and
  `checkout .` (`security.profile = "strict"` refuses); `gh auth status --show-token` is blocked next to `gh auth token`. The managed
  baseline pre-approves read-only `gh` calls, asks before Claude edits `.claude/hr-verify.toml` or `.claude/settings*.json`, and
  denies edits of `~/.claude-hr/state`.
- **Hardened trust path:** `verify trust` has no `--yes` and refuses a Claude Code shell or a non-terminal; `verify run` of an untrusted
  recipe is refused for an agent; the bash guard denies an agent running `verify trust` / `untrust` or touching the trust record.
- **The formatter step never runs repository-supplied code on its own:** a formatter that lives inside the repository (`.venv`,
  `node_modules/.bin`) and prettier (its configuration can load code, and no check on it survived review) need a trusted
  repository (an approved verify recipe) or `format.repo_tools = "always"`; `claude-hr-ctl fmt` marks such a formatter `NOT USED`.
- `doctor` now reports a foreign `ANTHROPIC_BASE_URL` proxy and Headroom Desktop (they override claude-hr's per-launch routing; it
  used to say OK), a missing `claude-hr-hook` on PATH, the current repository's verify recipe state, and security-guidance reviews that
  were skipped for lack of API credentials.
- New config sections `[gates]`, `[format]`, `[verify]`, `[audit]`, `[github]`, `[footprint]` and key `security.stop_review`
  (see `src/claude_hr/data/defaults.toml`). The weekly `maintain` job prunes the audit trail.
- `hr-status`'s description is now a safe YAML scalar. The repo itself is formatted with `ruff format` (line endings LF,
  `.git-blame-ignore-revs`), `.editorconfig` added.
- Tests: every test now runs sandboxed by default (a hook test had written a crash log into the real `~/.claude-hr`); integration
  tests drive the real stop gate, formatter and verify runner in a git repository.
- Found by independent adversarial reviews of this release (a 97-finding multi-agent review with reproduction and refutation, plus per-commit
  security reviews), all fixed unless noted:
  - **Trust path:** `verify trust` shows both tiers in full (a `[[full]]` check that shares a name with a `[[fast]]` one is no longer hidden), escapes terminal and
    bidi characters, and hashes exactly the bytes it showed; `verify untrust` works on an invalid or missing recipe; `hr-status` no longer pre-approves every
    `claude-hr-ctl` subcommand (an explicit read-only list, enforced by a test).
  - **Planted binaries:** a `git.exe` committed to a cloned repository ran when `init-repo`, `audit`, `fmt` or the stop gate called git on Windows. Bare commands
    are now resolved through absolute PATH entries only (`procutil.resolve_exe`), `taskkill` by absolute path.
  - **Formatter:** in-repo formatter binaries and prettier (its configuration can load code) need a trusted repository; writes are atomic; config reads are bounded; the clean
    gate follows renames and compares line endings like with like; wider environment scrub.
  - **Gate:** the block cap holds when a failing check writes files; one 150 s budget for the whole hook (the plugin's 170 s kill dropped blocks silently); the untracked-file
    fingerprint cap is gone; leftover children of a check are ended; after the cap the user is told the failure is still there (`systemMessage`) and the trail records why a
    gate skipped or errored; hooks read stdin as UTF-8 and tolerate a BOM (a Windows PowerShell 5.1 pipe made the secret guard silently allow everything).
  - **Audit:** case-insensitive on every OS, per-file isolation (one hostile JSON no longer hides the rest), agent-surface files are never lost to the 50,000-file cap,
    linear regexes, tag-character and hidden-Unicode coverage, exit code 2 when it could not run, file names no longer echoed, many more settings keys and hook patterns.
  - **init-repo / workflows:** generated CI installs what it runs and no longer fails on a fresh repo; `.gitattributes` / `.editorconfig` respect CRLF scripts and tabs;
    existing issue templates win; `dryRun` is fail-safe; a crashed integrator is reported as needing inspection; wildcard file lists make groups conflict.
  - **Wiring:** after a self-update the next update job re-applies the managed settings and plugins (and `doctor` says when they are stale); the state-directory deny rule
    follows `CLAUDE_HR_HOME` and `config.toml` asks; `uninstall`, `doctor` and `footprint` no longer crash or mislead on corrupt files; the audit trail append is locked on Windows.
  - **Second review round** (49 more findings in the fixes themselves and the gaps they left; fixed unless noted):
    - the unattended post-upgrade re-apply is scoped down to the settings baseline and the CLAUDE.md block (no plugin installs, no `-y`), keyed per step in `state/applied-version.json`
      and bound to the Claude config directory setup applied it to; `uninstall` removes the markers so an uninstalled baseline is not resurrected;
    - `verify init --write` no longer follows a symlink committed in the repository; recipe and manifests are read only if they are regular files; the checks' environment keeps
      `PWD`, `AWS_REGION`, `GOPRIVATE` and non-secret npm settings (`verify.env_allow` for the rest); a hang that equals its own `timeout_s` is a failure, and a run cut short by
      the shared budget tells the user instead of passing silently; a second session no longer runs the suite concurrently; a missing tool no longer disables the checks behind it;
    - the audit: a symlink to an ancestor (`.claude -> .`) cannot hide the agent surface, the hidden-Unicode scan has no 20,000-character budget a decoy can exhaust, hooks are judged by
      their arguments, unlistable directories and incomplete scans are findings, `.gitignore` matching is linear, and ordinary repositories no longer fail `--strict` on documented sample
      credentials, stock workflow expressions or ordinary CLAUDE.md rules;
    - the launcher resolves `claude`, `op`, the limiter prefix and `headroom wrap` through absolute PATH entries only (a planted `op.exe` ran on Windows); an untrusted repository
      falls back to the formatter on PATH instead of being shadowed by a refused in-repo copy; Windows atomic replace retries when a file is briefly in use;
    - init-repo works on a git top level only, skips the CI skeleton when another workflow already runs tests, installs corepack for pnpm/yarn, and runs pytest only when tests exist;
      the workflows validate their arguments, label unverified findings, and report a crashed integrator as needing inspection;
    - the Windows schedule script escapes apostrophes in paths, the statusline never fails on odd payloads, the audit-trail lock costs nothing where the OS cannot grant one, and
      `tests/test_schedule.py` no longer executes the scripts it only meant to parse (it could register or remove real Task Scheduler tasks on a Windows machine).
  - Accepted and documented: the formatter formats every file git reports as changed (your own uncommitted work included); an edit made by someone else while the checks run
    is verified on the next change; `config set` and the `CLAUDE_HR_*` switches are protected by Claude Code's permission prompts only (the audit flags them in project settings).
- Rejected after research, with reasons in `docs/ADDONS.md`: Headroom Desktop (mutually exclusive with claude-hr), `bun fmt` as a
  dependency, spec-driven frameworks, a GitHub MCP server by default, an LLM review Action on untrusted PRs, per-edit hooks,
  model-judged Stop hooks. Deferred: unattended LLM routines.

## 0.1.3 (2026-10-16)
- License is now MIT.
- **Windows self-update fixed.** `update` running `self` and then `plugins` in one process left the tool environment without
  site-packages (every `claude-hr-ctl` call failed with `ModuleNotFoundError`), and the detached reinstall helper was killed with
  uv's `claude-hr-ctl.exe` trampoline job. The reinstall now starts last, from a one-shot Task Scheduler task (outside that job) that
  waits for the updater to exit, recreates the `UV_*` environment, logs to `logs/self-update.log` and removes itself.
  Regression test: `tests/windows/verify-windows-selfupdate.ps1`.
- `setup --profile` (and the installers' `--profile` / `-Profile`) is now saved to `~/.claude-hr/config.toml`; before, the next
  `doctor`, `plugins sync` or `setup` silently went back to `standard`.
- `uninstall` no longer prints `schedule: FAILED` on Windows when no scheduled jobs exist.
- `doctor` warns when Linux per-session memory caps are inactive (no systemd user session bus / `dbus-user-session`).
- `install.sh` now adds `~/.local/bin` to your shell startup files when it is missing from PATH (as uv's own installer does; opt out
  with `--no-modify-path`), and says to log in on first run.
- The `headroom` MCP tool uses the same workspace as the proxy (`~/.claude-hr/headroom`) instead of `~/.headroom`.
- `security-reviewer` is limited to Read/Grep/Glob (Claude Code ignores `Bash(git diff *)` filters in a subagent `tools` list, so
  it had full Bash); `hr-parallel-worktrees` is manual-only (`disable-model-invocation`). New tests check that skill and agent
  names match their files and that agents do not grant Bash.
- README rewritten after executing it on Windows and Linux: first-run login, PATH, `--shim`, `--dry-run`, `--no-headroom-install`,
  install sizes (about 4 GB on Linux x86_64), uninstall, profiles, fork-based team extension, corrected command table.
- Tests can no longer register a scheduled task or start a reinstall on the machine running them.
- Found by an independent review of this release, all fixed:
  - a self-update whose reinstall failed or was killed was never retried (the checkout had already moved); it now is, `uv` is
    resolved before the checkout moves, and `doctor` warns about it;
  - on Linux the scheduled update job killed its own reinstall (systemd `KillMode=control-group`): the reinstall is now a
    transient systemd user unit, the units use `KillMode=process`, and launchd jobs set `AbandonProcessGroup`;
  - the Windows helper script no longer copies credentials from `UV_*` variables to disk (only `PATH` and non-secret `UV_*`), deletes
    itself, logs in one encoding and quotes typographic apostrophes safely;
  - re-running the installer kept a stale local `main` instead of `origin/main`, ignored an explicit `--repo`, and failed after a
    `--local` install; `install.ps1` no longer closes your PowerShell window on error or shadows `$PROFILE`;
  - `claude-hr-ctl uninstall` removed every claude-hr scheduled task on the machine; it now removes only this install's tasks,
    the self-update helper and orphans, and scheduler failures are reported as errors;
  - `config.toml` writes are valid TOML for any value (emoji), keep comments when nothing changes, tolerate a UTF-8 BOM, and
    report errors; `config set` without a value is rejected; an install-time `FOOTPRINT=lite` is saved;
  - `wt rm --delete-branch` says whether the branch was deleted or kept (unmerged);
  - README corrections: install sizes (download vs disk), `--dry-run`, macOS first login, the Updates row, savings wording.

## 0.1.2 (2026-10-15)
- An install upgraded from 0.1.0 now becomes owner-only on the first `claude-hr` or `claude-hr-ctl` command (found by testing the real
  v0.1.0 -> v0.1.1 self-update on a fresh user): `paths.tighten_existing()` runs on every entry point and creates nothing.

## 0.1.1 (2026-10-15)
Security hardening from the post-release commit review:
- `~/.claude-hr` and its `state/`, `logs/`, `backups/`, `cache/` folders are now owner-only (0700) on Linux/macOS; they hold the
  proxy log, the transcript-derived `headroom learn` report, usage data and copies of `settings.json`. `install.sh` creates the home
  folder 0700 from the start, and the proxy log is created 0600.
- Files claude-hr owns (learn report, Serena pin, proxy state file, proxy log) are never written or opened through a symlink; a symlink
  planted at such a path is replaced or refused instead of followed. Managed edits to your own `settings.json` still follow a dotfiles symlink.
- The proxy log handle is closed in the launcher after the proxy is spawned.

## 0.1.0 (2026-10-15)
Hardened after an independent review and real-OS testing (Windows 11 incl. PowerShell 5.1 and Task Scheduler, Ubuntu 22.04 with systemd/cgroups):
- Launcher: `claude --help`-derived subcommand/flag classification, best-effort bookkeeping, nesting guard, gateway detection,
  negative cache for a broken proxy, lazy imports (0.78 s -> 0.24 s on Windows), no-login launches direct.
- Windows: real `claude.exe` shim (no `.cmd`), scheduled jobs under `pythonw`, no console flashing, Job Object tests, installer fixes for PS 5.1.
- Updater: forward-only self-update, release tags only, defers while sessions or daemon workers run, rollback restores with the same
  dependency cutoff and is re-verified, 5-day cooldown.
- Guard hook: ~0.16 s per call via `claude-hr-hook`, far fewer false positives, PowerShell tool covered.
- Safety: Headroom stop never signals foreign processes; crontab never overwritten when unreadable; MCP servers only removed if claude-hr added them;
  CLAUDE.md block repair never eats user text; atomic writes follow symlinks and keep modes; cleanup fails closed.

- Initial release: `claude-hr` launcher, `claude-hr-ctl` control CLI, Headroom persistent proxy management, ownership-tracked
  settings merge, CLAUDE.md baseline + linter, plugin profiles + audit, secret-leak guard, resource governor
  (admission, cgroup / Job Object caps), scheduled monitor/update/maintain jobs, staged updater with rollback,
  Windows / Linux (systemd + cron) / macOS (launchd) installers.
