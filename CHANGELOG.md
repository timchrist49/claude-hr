# Changelog

## 0.2.0 (2026-10-16)
A verified development loop with no always-on token cost, for sessions run at Ultracode effort on `opusplan` (claude-hr still sets
no model, effort or thinking value). Plan: `docs/plans/2026-10-16-dev-workflow.md`; reasons: `docs/DESIGN.md` section 13.
- **New plugin `hr-gates`** (in the `standard` profile): one `Stop` hook, `claude-hr-hook stop`, once per turn. It formats the files
  Claude changed with the formatter the repo already configures (ruff/black, prettier/biome, gofmt, rustfmt), runs the repo's trusted
  fast checks and returns a failure to Claude, and appends a metadata-only audit trail. It adds no context unless a trusted check
  failed, skips plan mode and subagents, and fails open on every error.
- **New commands:** `fmt`, `verify init|status|trust|untrust|run`, `audit`, `init-repo`, `footprint`, `trail`. `verify` runs only a
  recipe (`.claude/hr-verify.toml`) whose exact bytes a person trusted; `audit` is a read-only, zero-token audit of a repository's
  agent surface, workflows and secrets; `init-repo` scaffolds CI (job `verify`), Dependabot, issue forms, a PR template,
  CODEOWNERS, `docs/plans`, `REVIEW.md` and `.editorconfig` (dry run by default, never overwrites); `footprint` estimates each
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
