# Operations runbook

## Daily checks
```
claude-hr-ctl status            # proxy, savings, plan usage, sessions, RAM, last-24h tokens, CLAUDE.md size
claude-hr-ctl doctor            # every check with the exact fix; exit 1 if anything is FAIL
claude-hr-ctl doctor --deep     # also measures ~/.claude size (time-boxed)
```

## Turning things off (in order of blast radius)
| Need | Do |
|---|---|
| One session without Headroom | `claude-hr --hr-direct …` (or `CLAUDE_HR_DIRECT=1`) |
| Everything without Headroom | `claude-hr-ctl config set headroom.enabled false` |
| Reinstall while sessions are open (Windows) | blocked on purpose: replacing the tool env under a running launcher breaks it. Close sessions, or `$env:CLAUDE_HR_FORCE_INSTALL=1` |
| Run without Headroom routing | use `claude` (claude-hr never edits its binary; only the optional `shim` shadows it: `claude-hr-ctl shim remove`). Plain `claude` still reads the managed `settings.json`, the CLAUDE.md block, the plugins and the MCP servers: only the Headroom routing is skipped. To remove those too, run `claude-hr-ctl uninstall`. |
| Stop scheduled jobs | `claude-hr-ctl schedule remove` |
| Undo all managed config | `claude-hr-ctl uninstall` (your own settings/plugins/CLAUDE.md content stay; an unparsable `settings.json` or `~/.claude.json` is reported and that step skipped, everything else still runs, the exit status is 1 and the output says the uninstall is INCOMPLETE; with `--purge` the claude-hr home is kept in that case because it holds the ownership record: fix the file and run it again) |
| Also delete Headroom and `~/.claude-hr` | `claude-hr-ctl uninstall --purge` |

## The development loop (`hr-gates`, `verify`, `audit`, `init-repo`, `footprint`)
```
claude-hr-ctl verify status            # is this repository's recipe trusted? what would the gate run?
claude-hr-ctl verify init --write      # propose .claude/hr-verify.toml from what the repo declares; then read it
claude-hr-ctl verify trust             # in a normal terminal, never inside Claude Code
claude-hr-ctl trail --tail 20          # what the end-of-turn gate did (metadata only)
claude-hr-ctl audit [--strict]         # read-only repository audit, zero tokens
claude-hr-ctl init-repo [--apply]      # scaffold CI, Dependabot, issue/PR templates, docs/plans + decisions, REVIEW.md, .editorconfig, .gitattributes
claude-hr-ctl footprint                # always-on token cost of the plugins, against footprint.budget_standard_tokens
```
The gate runs once per turn, only for a git repository, and not at all in plan mode or for subagents. It skips a working
tree that did not change since it last passed. A failing trusted check blocks the turn's end at most `verify.gate_max_blocks`
(2) times for the same tree, then lets Claude stop and names the failure (Claude Code itself gives up after 8 consecutive
blocks). Turn it off: `CLAUDE_HR_GATES=off` for one session, `gates.enabled = false` everywhere, `format.enabled` or
`verify.gate` for one step, `claude-hr-ctl verify untrust` for one repository.

`hr-plan-execute` runs the independent steps of an approved plan in parallel git worktrees. Commit the plan first (worktrees
start from the last commit; the run stops when tracked files are dirty). Run `claude-hr-ctl workflows install`, then ask Claude to
run the `hr-plan-execute` workflow with args `{plan: 'docs/plans/<file>.md', dryRun: true}` to see the groups, and again
without `dryRun`. It uses up to about 25 agents (`maxParallel` 4 by default, 8 at most). Passing groups are merged one by
one on the current branch; failed or conflicting groups are reported and left on their `hr/<planslug>-<group>` branches
(clean up with `claude-hr-ctl wt rm` or `git branch -d`). Its LLM review findings are advisory; CI is the gate. Statuses: `checks-unverified` (merged, but the full checks were not confirmed: an empty or missing check list or a dead checks agent), `checks-failed` also covers a reported pass that lists a failing command; the branch state comes from `git symbolic-ref -q HEAD`. `dryRun` defaults to a real run only when omitted: only `false` / `'false'` opts in to a real run, any other value is a dry run and the report says so; the dry run prints the plan's whole-change checks and follow-ups. A detached HEAD stops the run before any merge (status `detached-head`); a crashed or partial integrator is reported as `needsInspection` (merges may already be on your branch: look at it before anything else).

`init-repo` writes the CI job `verify` (use that name in a branch-protection rule). Its actions are referenced by tag with a
comment to pin them to a commit SHA; the Dependabot file it also writes keeps them updated. Review it before committing.
A token that lacks the `workflow` scope cannot push `.github/workflows/*`.

### Configuration reference for the development loop
All keys live in `~/.claude-hr/config.toml` (or `/etc/claude-hr/config.toml`, or `CLAUDE_HR_<SECTION>__<KEY>` in the environment); defaults and comments are in `src/claude_hr/data/defaults.toml`.

| Key | Default | What it does |
|---|---|---|
| `gates.enabled` | true | the Stop hook runs at all (also `CLAUDE_HR_GATES=off`) |
| `gates.protect_config` | true | the settings baseline asks before Claude edits `.claude/hr-verify.toml`, `.claude/settings*.json` and claude-hr's `config.toml`, and denies edits of its `state/`; off removes those rules |
| `gates.trail`, `gates.trail_days` | true, 90 | the metadata-only audit trail; month files older than this are pruned by `trail --prune` and the weekly job |
| `format.enabled` | true | the formatter step |
| `format.timeout_s` | 30 | cap for the formatter inside the hook's 150 s budget (it gets at most half of what is left) |
| `format.max_files`, `format.max_file_kb` | 40, 512 | files formatted per turn, largest file formatted |
| `format.skip_globs`, `format.allow` | [], [] | paths never formatted, formatter names allowed (empty = any detected) |
| `format.clean_gate` | true | rewrite a tracked file only if its committed version was already formatted |
| `format.repo_tools` | "trusted" | in-repo formatter binaries and prettier need a trusted repository; "always" lifts that |
| `verify.gate` | true | run the trusted recipe at the end of the turn |
| `verify.tier` | "fast" | which tier runs unattended (`full` runs the whole list on every turn) |
| `verify.gate_max_blocks` | 2 | blocks per unchanged tree before the gate lets Claude stop and tells you |
| `verify.gate_timeout_s` | 120 | cap for all checks together inside the 150 s budget (`format.timeout_s` + this above about 150 s buys nothing) |
| `verify.env_allow` | [] | names (case-insensitive) kept in the checks' environment although the credential deny-list would drop them, e.g. `DATABASE_URL`, `AWS_PROFILE`, `DJANGO_SECRET_KEY`; set by you, never by a repository |
| `verify.test_guard` | true | mention removed asserts and new skip markers in a failing gate's message |
| `audit.tools`, `audit.timeout_s` | true, 60 | `audit --tools` may run installed scanners, per-scanner limit |
| `github.readonly_allow` | true | the baseline pre-approves read-only `gh` calls |
| `security.stop_review` | "off" | `off` (also false, 0, no, disabled) sets `ENABLE_STOP_REVIEW=0`; `plugin-default` (also on, true, 1, yes, enabled) leaves security-guidance's per-turn Opus review on |
| `footprint.budget_standard_tokens` | 1500 | `footprint --strict` and `doctor` compare the standard profile's always-on estimate with it |
| `skills.max_description_chars` | 400 | `skills` and `plugins audit` flag longer listed descriptions |

`doctor` reports, among the existing checks: the recipe state of the current repository (from any subdirectory), a missing `claude-hr-hook` on PATH, a foreign proxy or Headroom
Desktop, `security-guidance` reviews that were skipped for lack of API credentials, `managed settings` (WARN when the baseline was applied by another claude-hr version or
`setup --only settings` would still change something; FAIL on an unparsable `state/settings-merge.json`), `footprint` (the always-on token estimate against
`footprint.budget_standard_tokens`, standard profile only), an unparsable `~/.claude.json` and an unrecognised `security.stop_review` value.

## Updates
```
claude-hr-ctl update --check                 # what is available, changes nothing
claude-hr-ctl update                         # apply now (defers while sessions run; --force to override)
claude-hr-ctl update --only headroom         # one component
```
Daily job: 03:17 local (`updates.at`). Log: `~/.claude-hr/logs/update.jsonl`. Pin instead of tracking:
`claude-hr-ctl config set claude.pin 2.1.292` / `headroom.version 0.40.0`. Release channel for Claude: `claude.channel`
(`stable` ≈ a week behind `latest`; the updater never downgrades you off a newer build unless `--allow-downgrade`).
claude-hr itself updates to the newest `vX.Y.Z` tag (`self.update_ref`); cut a release with `git tag vX.Y.Z && git push --tags`.

**After a self-update:** the daily `update` job (and a full `claude-hr-ctl update`, not `--check` or `--only`) re-applies the managed settings baseline and the CLAUDE.md block once per release, and nothing else: it never installs plugins, never adds marketplaces and never passes `-y` (a plugin a release adds, such as `hr-gates` after an upgrade from 0.1.x, is reported by `claude-hr-ctl doctor` with the fix `claude-hr-ctl plugins sync`). It re-applies only a step that `setup` recorded in `state/applied-version.json` (`{settings, claude-md, plugins, config_dir, ts}`: a step you skipped with `--skip` or `--only` stays skipped), and only when the job resolves the same Claude config directory that setup applied it to, because a scheduled job does not inherit the `CLAUDE_CONFIG_DIR` of your shell; otherwise it logs one `deferred` `baseline` row a day in `logs/update.jsonl` / `job-update.log`, and `doctor` says to run `claude-hr-ctl setup`. A machine set up before 0.2.0 has no per-step record yet, so run `claude-hr-ctl setup` once. `uninstall` removes `state/install.json` and the applied-version record.

**Rollback by hand:** `claude install <version> --force`; `uv tool install --python 3.13 "headroom-ai[all]==<ver>"`;
`git -C ~/.claude-hr/repo checkout <tag> && uv tool install --force --reinstall ~/.claude-hr/repo`.

**A self-update that did not finish:** claude-hr's own reinstall runs in the background after `update` exits and logs to
`~/.claude-hr/logs/self-update.log` (Windows: a one-shot Task Scheduler task `selfupdate` in the `claude-hr` folder that deletes
itself and its script; Linux: a transient `claude-hr-selfupdate-*` systemd user unit). If `doctor` warns that the installed
version is behind the checkout, run `claude-hr-ctl update --only self` to retry (the next daily update retries too), or re-run
the installer.

## Monitoring and alerts
`monitor tick` (every 15 min) writes `state/health.json`, appends `logs/monitor.jsonl`, restarts a dead proxy, and alerts
(once per 6 h per condition) on: low disk, high RAM, large `~/.claude`, proxy down. The weekly `maintain` job also runs
`creds scan` over the last 7 days of transcripts and sends a one-off `transcript-secrets` alert through the same channels.
Channels (set any): `monitor.alert_ntfy_url` (phone push), `monitor.alert_webhook_url`, `monitor.alert_command`
(argv, no shell; text arrives in `CLAUDE_HR_ALERT_TITLE` / `CLAUDE_HR_ALERT_BODY`). Claude's own phone push needs Remote Control,
which is disabled behind a proxy, hence ntfy.

## Scheduler specifics
- **Windows**: Task Scheduler folder `\claude-hr\`, runs `pythonw.exe` (no console window), only while you are logged on.
- **Linux**: `systemd --user` timers. On a headless VM run once as root: `loginctl enable-linger <user>`. Without
  systemd (containers) it falls back to a marked cron block. `systemd-run --user` caps also need `dbus-user-session`.
- **macOS**: LaunchAgents `io.github.timchrist49.claude-hr.*`.

## Resource governor tuning (`~/.claude-hr/config.toml`)
```toml
[resources]
max_sessions = 4            # or "auto"
memory_max_gb = 8           # per session tree; 0 disables hard caps
low_priority = true         # below-normal CPU priority
[headroom]
footprint = "lite"          # skip ML/memory extras (≈ 600 MB less RAM, far smaller download)
```
CPU quotas on Linux need the `cpu` controller delegated to user managers (one-time, root):
`/etc/systemd/system/user@.service.d/delegate.conf` → `[Service]\nDelegate=cpu cpuset io memory pids`, then
`systemctl daemon-reload` and re-login; then set `resources.cpu_quota_pct`.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `[claude-hr] … continuing WITHOUT compression` | Proxy not healthy. `claude-hr-ctl headroom status`, `claude-hr-ctl headroom logs`, `claude-hr-ctl headroom start --wait 180`. First start after install downloads models. |
| Claude says it cannot reach the API | A stale `ANTHROPIC_BASE_URL` in your shell or settings. `claude-hr-ctl doctor` names it. |
| Background sessions not compressed | The Claude daemon was started by a plain `claude`. `claude-hr-ctl daemon status`; `claude-hr-ctl daemon restart` when idle. |
| Subscription limit burning faster than expected | `ANTHROPIC_API_KEY` exported (overrides login — `creds doctor`), or many parallel sessions: lower `resources.max_sessions`. `claude-hr-ctl usage --by project`. |
| `/rc` (Remote Control) unavailable | Disabled by Claude Code behind any custom base URL. `claude-hr --hr-direct`. |
| High RAM | Proxy ~1.2 GB + ~0.3–0.7 GB per session. `footprint = "lite"`, fewer sessions. |
| `~/.claude` huge | `claude-hr-ctl cleanup` (dry-run) then `--apply`. |
| Plugin token cost too high | `claude-hr-ctl plugins audit`; move big ones to project scope (`plugins suggest`). |
| Claude shows a non-blocking hook notice every turn | `claude-hr-hook` is not on PATH in the shell Claude runs in: `uv tool update-shell`, open a new terminal (`doctor` says so). |
| The gate never runs the tests | `verify status`: no recipe, not trusted, or changed since trusted. `verify init --write`, read it, `verify trust` in a terminal. `trail` shows what the last turns did. |
| `trail` shows `note=another gate run in progress` or `note=cannot start` | The first: a second session's Stop found the per-repository lock (`state/verify-run-<hash>.lock`, stale after max(180 s, timeout + 60 s) or when its owner is gone); it is retried at the next Stop. The second: a check's tool is missing: the other checks still ran, the tree is remembered until it changes; install the tool or edit the recipe. |
| `trail` shows `note=time budget` | The hook's one 150 s budget (git, formatter and checks together; the plugin kills the hook at 170 s) ran out. If no check started the turn is retried next time; a run that was cut short is remembered, so an over-long suite is not started again on every Stop. Move slow checks to the `full` tier. |
| The gate blocks again and again | After `verify.gate_max_blocks` blocks for the same tree it stops and names the failure; fix it or `verify untrust`. A check that is flaky or slow belongs in the `full` tier, not `fast`. |
| A file was not formatted | `fmt` lists the formatter the repo configures and marks one as `NOT USED` with the reason. A formatter inside the repo (`.venv`, `node_modules/.bin`) or prettier (its configuration can load code) is used only after `verify trust` for that repository (or `format.repo_tools = always`). On Windows a file another program holds open (an editor, an antivirus scan) cannot be replaced atomically: after a short retry it is left unformatted and the report says `cannot write: file in use by another process` (never truncated or half-written; close the program or run `claude-hr-ctl fmt <path>`). In a repository that is not trusted yet a ruff or black on your PATH is used even when the project has its own copy in `.venv`. A tracked file whose committed version was never formatted is left alone on purpose (`format.clean_gate = false` to change that); `trail` records skips. |
| Hook errors after uninstalling claude-hr | `hr-core` hook commands exit 0 when `claude-hr-hook` is absent. The `hr-gates` Stop hook (exec form, no shell) shows a non-blocking notice each turn until the plugin is removed: `claude-hr-ctl uninstall` does that, or `claude plugin uninstall hr-gates@claude-hr`. |
| Windows: long-path clone failures in plugin install | `git config --system core.longpaths true`, or shorten `CLAUDE_CONFIG_DIR`. |

## Moving to a new VM
Run the installer; copy `~/.claude-hr/config.toml` if you have local overrides. Nothing else is machine-specific.
For a fleet, put shared overrides in `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`).
