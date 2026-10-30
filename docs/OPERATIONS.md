# Operations runbook

## Daily checks
```
claude-hr-ctl status            # proxy, savings, plan usage, extra-usage dollars, sessions, RAM, last-24h tokens, CLAUDE.md size
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
claude-hr-ctl verify status            # is this repository's recipe trusted? was the folder offered, declined? what would the gate run?
claude-hr-ctl verify enable            # turn the gate on here (what the launcher offers once per folder): runs the checks once, keeps the passing ones, asks you
claude-hr-ctl verify disable           # turn it off here, keep the file, stop the launcher asking
claude-hr-ctl verify init --write      # the manual way: propose .claude/hr-verify.toml from what the repo declares; then read it
claude-hr-ctl verify trust             # in a normal terminal, never inside Claude Code (enable does this for you)
claude-hr-ctl trail --tail 20          # what the end-of-turn gate did (metadata only)
claude-hr-ctl audit [--strict]         # read-only repository audit, zero tokens
claude-hr-ctl init-repo [--apply]      # scaffold CI, Dependabot, issue/PR templates, docs/plans + decisions, REVIEW.md, .editorconfig, .gitattributes
claude-hr-ctl footprint                # always-on token cost of the plugins (measured; no cap unless footprint.budget_standard_tokens is set)
```
The gate runs once per turn, only for a git repository, and not at all in plan mode or for subagents. It skips a working
tree that did not change since it last passed. A failing trusted check blocks the turn's end at most `verify.gate_max_blocks`
(2) times for the same tree, then lets Claude stop and names the failure (Claude Code itself gives up after 8 consecutive
blocks). Turn it off: `CLAUDE_HR_GATES=off` for one session, `gates.enabled = false` everywhere, `format.enabled` or
`verify.gate` for one step, `claude-hr-ctl verify disable` for one repository (`verify untrust` revokes the approval but the launcher then offers the file again next time).

**The one-time offer.** The first time you start an interactive `claude-hr` session in a git repository, in a normal terminal, in a folder Claude Code itself already trusts (its own "do you trust this folder?" answer, recorded for the repository's top folder), the launcher asks once whether to turn the gate on (`Turn it on? [Y/n]`; keys typed before the question appears are discarded and five minutes without an answer count as "not now"). It is never asked in `-p`, `agents`, subcommands, a Claude Code shell, with piped input or a redirected stderr (the question is written there), in a home directory, in a bare repository, for a folder that is trusted, invalid, or already answered, or when nothing in the folder tells claude-hr what to run. A yes runs each discovered fast check once (a check gets at most 60 s, the step 120 s, with a scrubbed environment and no shell), keeps only those that exit 0 today, writes them to `.claude/hr-verify.toml`, adds that path to `.git/info/exclude` (only when `.git` is a directory; local to the clone) and trusts the file's exact bytes. A check that edits tracked files while it runs once is left out. A no is remembered in `state/verify-offer.json` (200 folders at most), and so is "nothing to propose here" (until a project file changes); Ctrl-C, EOF or two unclear answers are "not now" and record nothing; a bare Enter on a default-No screen, a yes where no check passes today and an unanswered question are remembered as "later" (7, 3 and 1 days: `verify status` says when it asks again; `verify enable` ignores it). A recipe that ships with the repository is shown in full and asked about with No as the default; if it changes after a no, you are asked again. The question adds about 3 ms to an interactive start in a folder that is already trusted or answered and nothing to any other launch. `verify.offer = "off"` (or `CLAUDE_HR_VERIFY__OFFER=off`) never asks; `claude-hr-ctl verify enable` still works.

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
| `verify.offer` | "ask" | ask once per git folder, at the start of an interactive `claude-hr` session in a normal terminal, whether to turn the gate on; `off` never asks (`verify enable` still works). Needs `gates.enabled` and `verify.gate`; the `minimal` plugin profile has no gate, so nothing is asked there. Nothing answers the question for you |
| `verify.tier` | "fast" | which tier runs unattended (`full` runs the whole list on every turn) |
| `verify.gate_max_blocks` | 2 | blocks per unchanged tree before the gate lets Claude stop and tells you |
| `verify.gate_timeout_s` | 120 | cap for all checks together inside the 150 s budget (`format.timeout_s` + this above about 150 s buys nothing) |
| `verify.env_allow` | [] | names (case-insensitive) kept in the checks' environment although the credential deny-list would drop them, e.g. `DATABASE_URL`, `AWS_PROFILE`, `DJANGO_SECRET_KEY`; set by you, never by a repository |
| `verify.test_guard` | true | mention removed asserts and new skip markers in a failing gate's message |
| `audit.tools`, `audit.timeout_s` | true, 60 | `audit --tools` may run installed scanners, per-scanner limit |
| `github.readonly_allow` | true | the baseline pre-approves read-only `gh` calls |
| `security.stop_review` | "off" | `off` (also false, 0, no, disabled) sets `ENABLE_STOP_REVIEW=0`; `plugin-default` (also on, true, 1, yes, enabled) leaves security-guidance's per-turn Opus review on |
| `footprint.budget_standard_tokens` | 0 | 0 = no cap (the default): `footprint` and `doctor` show the always-on estimate and nothing is refused or warned about. A number above 0 makes `doctor` warn, and `footprint --strict` exit 1, above it (standard profile only) |
| `skills.max_description_chars` | 400 | `skills` and `plugins audit` flag longer listed descriptions |

`doctor` reports, among the existing checks: the recipe state of the current repository (from any subdirectory), a missing `claude-hr-hook` on PATH, a foreign proxy or Headroom
Desktop, `security-guidance` reviews that were skipped for lack of API credentials, `managed settings` (WARN when the baseline was applied by another claude-hr version or
`setup --only settings` would still change something; FAIL on an unparsable `state/settings-merge.json`), `footprint` (the always-on token estimate for every profile; a WARN only if a cap is set with
`footprint.budget_standard_tokens`), an unparsable `~/.claude.json` and an unrecognised `security.stop_review` value.

## Updates
```
claude-hr update                             # update EVERYTHING now (same as claude-hr-ctl update; the daily job runs it too)
claude-hr update --check                     # preview: current -> target per component, changes nothing (still runs git fetch)
claude-hr update --only headroom plugins     # just these (self, claude, headroom, plugins, baseline, proxy; always in that order)
claude-hr update --no-restart                # never restart the Headroom proxy (a Headroom upgrade is then deferred)
claude-hr update --force                     # update even while claude-hr sessions run (Windows: not claude-hr itself, see "While it runs")
claude-hr update --claude-only [args]        # Claude Code's own `claude update`, untouched by claude-hr
```
A full run does, in order: claude-hr itself (a newer release tag: checked out now, reinstalled in the background), Claude Code
(staged, smoke-tested, rolled back on failure), Headroom (5-day cooldown, smoke test, rollback), user-scope plugins and their
marketplaces (never `-y`, never `marketplace add`, never an install outside the profile), then the post steps: re-apply the settings
baseline and CLAUDE.md block, restart the proxy only when no session runs and it was started with older settings, and a doctor
verdict. When claude-hr itself updates, the post steps run in the background helper right after its reinstall (`update --post`,
internal) and the result lands in `state/update-last.json` and `claude-hr-ctl status`; the helper's log is `logs/self-update.log`.
The report ends with `Everything is up to date.` or the list of what is pending/failed and why. **Exit codes:** 0 ok or current,
1 a component failed (also a failed check), 2 usage error, 75 deferred because claude-hr sessions or Claude background sessions
run, or another update is running (nothing is broken: close them, or `--force`; on Windows `--force` cannot replace claude-hr
itself under a running session). The scheduled job counts 75 as success.
With the `claude` shim installed (`claude-hr-ctl shim install`; run it again after upgrading to 0.3.0), `claude update` typed in a
terminal means the same as `claude-hr update`; scripts, IDEs and CI that call `claude update` still get Claude Code's own updater.
`update --check` cannot preview plugin updates (the update asks Claude Code for each plugin): it lists the installed versions.
**Not updated by any command:** uv, git, Python, the Hugging Face model cache, other uv tools, the Serena pin (changes with a
claude-hr release) and project-scope plugins.
Daily job: 03:17 local (`updates.at`). Log: `~/.claude-hr/logs/update.jsonl`. Pin instead of tracking:
`claude-hr-ctl config set claude.pin 2.1.292` / `headroom.version 0.40.0`. Release channel for Claude: `claude.channel`
(`auto` by default = your own `autoUpdatesChannel` from `~/.claude/settings.json`, else `stable`; `stable` ≈ a week behind `latest`; the updater never downgrades you off a newer build unless `--allow-downgrade`).
claude-hr itself updates to the newest `vX.Y.Z` tag (`self.update_ref`); cut a release with `git tag vX.Y.Z && git push --tags`.

**After a self-update:** the post steps (see above; the daily `update` job and a full `claude-hr update`, not `--check` or `--only`) re-apply the managed settings baseline and the CLAUDE.md block once per release, and nothing else: it never installs plugins, never adds marketplaces and never passes `-y` (a plugin a release adds, such as `hr-gates` after an upgrade from 0.1.x, is reported by `claude-hr-ctl doctor` with the fix `claude-hr-ctl plugins sync`). It re-applies only a step that `setup` recorded in `state/applied-version.json` (`{settings, claude-md, plugins, config_dir, ts}`: a step you skipped with `--skip` or `--only` stays skipped), and only when the job resolves the same Claude config directory that setup applied it to, because a scheduled job does not inherit the `CLAUDE_CONFIG_DIR` of your shell; otherwise it logs one `deferred` `baseline` row a day in `logs/update.jsonl` / `job-update.log`, and `doctor` says to run `claude-hr-ctl setup`. A machine set up before 0.2.0 has no per-step record yet, so run `claude-hr-ctl setup` once. `uninstall` removes `state/install.json` and the applied-version record.

**Rollback by hand:** `claude install <version> --force`; `uv tool install --python 3.13 "headroom-ai[all]==<ver>"`;
`git -C ~/.claude-hr/repo checkout <tag> && uv tool install --force --reinstall ~/.claude-hr/repo`.

**While it runs:** at a terminal, `update` lists each component on stderr as it starts (`updating Claude Code (a new version
takes about a minute) ...`); the one report still comes at the end. The scheduled job and pipes get no progress lines, only the
report and the log. When the report says claude-hr itself is "finishing in the background", the reinstall takes a minute or two
(up to four if a claude-hr program is slow to close): **do not open `claude-hr` again until `claude-hr-ctl --version` shows the
new version.** On Windows the helper first waits up to 2 minutes for the updater to exit, then up to 2 minutes for every running
`claude-hr` program; a session that is still running from the tool environment when that wait ends makes it leave claude-hr as it
is (it logs `NOT reinstalling`) rather than let uv fail halfway and leave the environment half-deleted. Nothing is lost: the
checkout is already on the new release, and once no claude-hr session runs, the next `claude-hr update` finishes the job at
once. While a session runs, a plain `update` answers `deferred` (exit 75) and `update --force` queues the reinstall again,
says so in the report, and the helper skips it again: close the session first.

**A self-update that did not finish:** claude-hr's own reinstall runs in the background after `update` exits and logs to
`~/.claude-hr/logs/self-update.log` (Windows: a one-shot Task Scheduler task `selfupdate` in the `claude-hr` folder that deletes
itself and its script; Linux: a transient `claude-hr-selfupdate-*` systemd user unit). `logs/update.jsonl` has a `self` row with
action `launched` once the helper was started and how (task scheduler, systemd unit or detached process; on Windows a detached
process is the fallback when the scheduler refuses, and it may not outlive the command); an `updated` row without it means the
run ended before that point (closed terminal, Ctrl-C). If `doctor` warns that the installed version is behind the checkout, run
`claude-hr-ctl update --only self` to retry (the next daily update retries too), or re-run the installer. A retry waits 15
minutes only after a reinstall WAS launched (`state/self-update.json`, written when the helper is launched; removed when the
launch fails or is interrupted and, on Windows, when the helper's reinstall did not happen; on Linux and macOS a reinstall that
fails inside the unit leaves it until the 15 minutes pass); a run that ended earlier can be retried at once. Last resort, from a
terminal with no `claude-hr` running:
`uv tool install --python 3.13 --force --reinstall ~/.claude-hr/repo`.

## Monitoring and alerts
`monitor tick` (every 15 min) writes `state/health.json`, appends `logs/monitor.jsonl`, restarts a dead proxy, and alerts
(once per 6 h per condition) on: low disk, high RAM, large `~/.claude`, proxy down. On Linux it also watches Claude's shared
background scope (see below) and the kernel's pressure stall information: `scope-full` (a scope holds `monitor.scope_warn_pct` = 90%
of its memory ceiling, the soft limit else the hard cap; reclaimable file cache is not counted), `scope-capped` (it reached its hard
cap) and `scope-oom` (it lost a process to the OOM killer), both counted since the previous tick, and `pressure` (memory or I/O
stalled tasks for `monitor.psi_warn_pct` = 20% of the time since the previous tick, from the kernel's cumulative stall counters, or
of the last minute). These alerts arrive shortly AFTER the event, within one `monitor.interval_minutes` (15; a changed interval
reaches the scheduler with `claude-hr-ctl schedule install`), and a frozen machine cannot run the tick at all. The monitor also
keeps the caps right for the machine (`monitor.heal_resource_caps`). The weekly `maintain` job also runs
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
extra_proxy_args = []       # extra flags for `headroom proxy`, e.g. ["--lossless"]
```
`headroom.extra_proxy_args` is passed to `headroom proxy` when claude-hr starts it, so a change applies the next time the proxy
starts (restart it only when no session is using it). `--lossless` is Headroom's no-CCR mode: it compresses tool output with
format-native lossless compaction and never emits a retrieval marker, so no `headroom_retrieve` call is needed; see
`headroom proxy --help` for the other flags.
CPU quotas on Linux need the `cpu` controller delegated to user managers (one-time, root):
`/etc/systemd/system/user@.service.d/delegate.conf` → `[Service]\nDelegate=cpu cpuset io memory pids`, then
`systemctl daemon-reload` and re-login; then set `resources.cpu_quota_pct`. Without that delegation systemd accepts `CPUQuota`
and ignores it; `doctor` says so (`cpu quota`) when the setting is on and a running scope has no CPU limit.

**Claude's background sessions share one scope.** `claude agents` / `--bg` run inside Claude's background service, which the
process wrapper (`~/.claude-hr/bin/hr-wrap.sh`, set as `CLAUDE_CODE_PROCESS_WRAPPER`) puts into ONE systemd scope,
`claude-hr-<pid>.scope`, together with every session it hosts and their MCP servers and test runs. Its caps are sized for the whole
group: `MemoryMax` = `memory_max_gb` x min(the session limit, 4) (the session limit is `resources.max_sessions`; `auto` = the RAM,
core and `session_cap` formula), at most 70% of RAM; `MemoryHigh` = 75% of that (`memory_high_gb = 0` turns the soft limit off: the
kernel then kills at the hard cap instead of throttling the allocating tasks above it, which is worse, see below); `CPUQuota` = `cpu_quota_pct` x the same count, at most all
cores. The background service keeps that scope, and its caps, for as long as it runs (days), so the numbers must follow the machine:
- the wrapper records what it was sized for (`# sized for:` line) and the monitor re-writes it when RAM, CPUs or `resources.*`
  change (more than 5%: a few MB of MemTotal noise is not a resize); `doctor` shows `agent caps` with both sizes while it is stale;
- the scope that is already running is RAISED to the new numbers with `systemctl --user set-property --runtime` (never given a cap
  it did not have; a soft limit the config no longer wants is lifted). A LOWER number (the VM shrank, or you tightened `resources.*`)
  applies to the next scope only: `doctor` flags the running scope as larger than the machine allows, and
  `claude-hr-ctl daemon restart` replaces it once no session is running;
- `doctor` lists each running scope with its real numbers (`agent scopes`: what it holds without reclaimable cache, with it, hard cap,
  soft limit, tasks, how often it went over the soft limit and reached the hard cap). `doctor` and the monitor cover this shared
  scope only; the scopes of sessions started through `claude-hr` itself (`run-*.scope`) are not listed.

**Why a soft limit at 75% of the cap, and not 90% or none** (measured, not assumed: a sealed 2 GB scope, swap off, with 3 "sessions"
holding 350 MB each and timing small allocations, plus 15 workers growing towards 3.3 GB of demand in total). With the soft limit at
75% the kernel slowed the workers to a crawl (the scope peaked at 1.8 GB), killed nothing, and the sessions' allocations took at most
2 ms. At 90% the narrow band could not hold the workers: they reached the hard cap after 16 s and the OOM killer killed a SESSION
(twice), because it picks the process with the most memory, not the runaway. With no soft limit it killed a session after 1.5 s
(5 kills in the run). At 50% the soft limit sat below the sessions' own memory and their allocations stalled for seconds. So the
band between the soft limit and the cap is the room the kernel has to slow a runaway down instead of killing the largest process;
narrowing it, or removing it (`memory_high_gb = 0`), trades a slow test run for a dead session. Keep the soft limit above what the
sessions themselves hold. The test used anonymous memory only (no file cache pressure, no disk I/O), so it does not rule out other
causes of a stall.

Session admission (`max_sessions`) counts the sessions started through `claude-hr`; sessions hosted by the background service are
bounded by their shared scope instead, not counted. Above the soft limit the kernel throttles the tasks that allocate memory, which
can make every session in the scope crawl; at the hard cap it kills a process. Bound heavy runs inside agent sessions: `jest
--maxWorkers=2`, `pytest -n 2` (needs pytest-xdist), `cargo test -- --test-threads=2` (`-j` only limits compile jobs). An unbounded
test run starts a worker per core.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `[claude-hr] … continuing WITHOUT compression` | Proxy not healthy. `claude-hr-ctl headroom status`, `claude-hr-ctl headroom logs`, `claude-hr-ctl headroom start --wait 180`. First start after install downloads models. |
| Claude says it cannot reach the API | A stale `ANTHROPIC_BASE_URL` in your shell or settings. `claude-hr-ctl doctor` names it. |
| Background sessions not compressed | The Claude daemon was started by a plain `claude`. `claude-hr-ctl daemon status`; `claude-hr-ctl daemon restart` when idle. |
| Subscription limit burning faster than expected | `ANTHROPIC_API_KEY` exported (overrides login — `creds doctor`), or many parallel sessions: lower `resources.max_sessions`. `claude-hr-ctl usage --by project`. Once the 7-day window is full, extra usage is billed at API prices: `claude-hr-ctl status` and `doctor` show the window and the extra-usage dollars (from Headroom's poll, so the proxy must have run), and the status line adds `7d` at 80% and the extra dollars when the window is full. Fast mode (`/fast`) doubles Opus prices; `fastModePerSessionOptIn` in settings.json keeps it from persisting across sessions. |
| `/rc` (Remote Control) unavailable | Disabled by Claude Code behind any custom base URL. `claude-hr --hr-direct`. |
| High RAM | Proxy ~1.2 GB + ~0.3–0.7 GB per session. `footprint = "lite"`, fewer sessions. |
| Linux VM crawls or freezes while several background (`claude agents`) sessions work; `ps` and `git` time out | Likely cause: a heavy run (a full test suite with a worker per core) in the memory-capped scope the background sessions share, or a cap sized for a smaller machine. `claude-hr-ctl doctor` (`agent caps`, `agent scopes`: near its ceiling? sized for a smaller machine?), `claude-hr-ctl monitor tick` to re-size now, then bound the runner (`--maxWorkers`). The `scope-*` and `pressure` alerts follow within one monitor interval. To see what the kernel did, the previous boot's log must be persistent (`Storage=persistent` in journald.conf): `sudo journalctl -k -b -1 --no-pager \| grep -i -E "oom\|hung_task\|Memory cgroup"`; your hypervisor's disk-latency graph covers the I/O side. |
| `claude-hr update` printed nothing for a minute and the version did not change | The report comes at the end, and claude-hr's own reinstall finishes in the background (see "While it runs" under Updates). Check `claude-hr-ctl --version` after two minutes; `logs/update.jsonl` shows whether the helper was `launched`, `logs/self-update.log` what it did. |
| Windows installer stops with `Invoke-RestMethod : Unable to connect to the remote server` (older installers, at the uv or Claude Code step) | That PC cannot open a connection to the host: a firewall, web filter or proxy, not a TLS or login problem. The installer now tries a second route (uv: its GitHub release, then winget; Claude Code: winget) and, when all fail, prints what it tried and which hosts answer. Give that list to whoever runs the network, or run `winget install --id astral-sh.uv -e`, open a new terminal and run the one-liner again. The installer is fetched from `main`, so no claude-hr release is needed to get the fix. |
| Windows installer: `... installed at <path>, but it will not run: Program 'claude.exe' failed to run: Access is denied` (older installers crashed with `iex : Program 'claude.exe' failed to run`) | The program is there but Windows will not start it: antivirus or application control (AppLocker, WDAC, an endpoint rule), or a scan that has not finished (the installer already tried four times over 15 s). In a normal PowerShell window run `& '<path>' --version` for Windows' own message, and have whoever manages the PC allow that file; then run the one-liner again. A Claude Code that winget installed is a portable `claude.exe` under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_*` reached through a link in `...\WinGet\Links`: the rule has to allow that folder. |
| `~/.claude` huge | `claude-hr-ctl cleanup` (dry-run) then `--apply`. |
| Plugin token cost too high | `claude-hr-ctl plugins audit`; move big ones to project scope (`plugins suggest`). |
| Claude shows a non-blocking hook notice every turn | `claude-hr-hook` is not on PATH in the shell Claude runs in: `uv tool update-shell`, open a new terminal (`doctor` says so). |
| The gate never runs the tests | `verify status`: no recipe, not trusted, changed since trusted, or declined ("declined on ..."). `claude-hr-ctl verify enable` in a normal terminal (or the manual way: `verify init --write`, read it, `verify trust`). `trail` shows what the last turns did. Also check `plugins.profile` is not `minimal` (no `hr-gates` hook there) and `claude-hr-hook` is on PATH (`doctor`). |
| `verify enable` turned on fewer checks than expected, or none | Only checks that pass today are kept (a gate that fails on problems that were already there only wastes Claude's time); the others are listed with the reason ("failing today", "could not start", "did not finish within Ns") and written into the file as comments. Fix them, or add them to `.claude/hr-verify.toml` yourself, then run `verify enable` again or `verify trust`. |
| `claude-hr` asked about the gate, and I do not want that | Answer `n` (remembered for that folder), or `claude-hr-ctl config set verify.offer off` for everywhere. Nothing is asked in `-p`, `agents` or subcommands. |
| An empty terminal window titled with the path of `headroom.exe` keeps popping up on Windows (and closing it stops the proxy) | claude-hr 0.2.1 or older started the proxy with `DETACHED_PROCESS`, which made Windows open a console for the python process uv's `headroom.exe` starts. Fixed in 0.2.2 (the proxy now owns a hidden console): `claude-hr-ctl update`, then `claude-hr-ctl headroom restart` when no Claude session is running. To check on your machine: `& "$env:APPDATA\uv\tools\claude-hr\Scripts\python.exe" "$env:USERPROFILE\.claude-hr\repo\tests\windows\proxy_window_probe.py" --restart --keep` (stops a running proxy, starts it again and watches for a new console window; exit 0 = none and healthy, 1 = a window appeared or the proxy did not come up, 2 = it did not run). The same fix covers the self-update helper (a one-shot task started through `conhost.exe --headless`) and the monitor's `alert_command`. If the proxy's hidden console host was killed from outside, `claude-hr-ctl headroom restart` brings it back. |
| `doctor` says `headroom settings` | The running Headroom proxy was started with other settings than the ones in force now (or by an older claude-hr), for example after 0.2.1 turned memory and the output shaper off. `claude-hr-ctl headroom restart` applies them; do it when no Claude session is running (sessions lose Headroom for up to a minute). |
| `claude-hr` never asks about the gate | It asks only in an interactive session with a real terminal on stdin, stdout and stderr (Windows Terminal, PowerShell, a Linux/macOS terminal; Git Bash's default mintty window looks like a pipe to Windows Python: use Windows Terminal or `winpty`), outside a Claude Code session, in a git work tree that is not your home directory and whose top folder Claude Code has already been told to trust (start `claude` there once and accept its trust question), with `verify.offer = "ask"`, `gates.enabled` and `verify.gate` on, a plugin profile other than `minimal`, and project files claude-hr recognises (package.json, pyproject.toml, Cargo.toml, go.mod, Makefile ...). `claude-hr-ctl verify status` shows whether the folder was already answered; `claude-hr-ctl verify enable` asks on request. |
| `claude-hr` asks again about a folder I declined | The folder's `.claude/hr-verify.toml` changed since you said no (a recipe that arrives with a `git pull` is shown to you before anything runs), or Ctrl-C/EOF at the question counts as "not now". `verify disable` records a no for the current file. |
| `.claude/hr-verify.toml` is missing from `git status` | `enable` adds it to `.git/info/exclude` so a generated file is never committed by accident. To share it with the team, remove that line and commit the file. |
| `trail` shows `note=another gate run in progress` or `note=cannot start` | The first: a second session's Stop found the per-repository lock (`state/verify-run-<hash>.lock`, stale after max(180 s, timeout + 60 s) or when its owner is gone); it is retried at the next Stop. The second: a check's tool is missing: the other checks still ran, the tree is remembered until it changes; install the tool or edit the recipe. |
| `trail` shows `note=time budget` | The hook's one 150 s budget (git, formatter and checks together; the plugin kills the hook at 170 s) ran out. If no check started the turn is retried next time; a run that was cut short is remembered, so an over-long suite is not started again on every Stop. Move slow checks to the `full` tier. |
| The gate blocks again and again | After `verify.gate_max_blocks` blocks for the same tree it stops and names the failure; fix it or `verify untrust`. A check that is flaky or slow belongs in the `full` tier, not `fast`. |
| A file was not formatted | `fmt` lists the formatter the repo configures and marks one as `NOT USED` with the reason. A formatter inside the repo (`.venv`, `node_modules/.bin`) or prettier (its configuration can load code) is used only after `verify trust` for that repository (or `format.repo_tools = always`). On Windows a file another program holds open (an editor, an antivirus scan) cannot be replaced atomically: after a short retry it is left unformatted and the report says `cannot write: file in use by another process` (never truncated or half-written; close the program or run `claude-hr-ctl fmt <path>`). In a repository that is not trusted yet a ruff or black on your PATH is used even when the project has its own copy in `.venv`. A tracked file whose committed version was never formatted is left alone on purpose (`format.clean_gate = false` to change that); `trail` records skips. |
| Hook errors after uninstalling claude-hr | `hr-core` hook commands exit 0 when `claude-hr-hook` is absent. The `hr-gates` Stop hook (exec form, no shell) shows a non-blocking notice each turn until the plugin is removed: `claude-hr-ctl uninstall` does that, or `claude plugin uninstall hr-gates@claude-hr`. |
| Windows: long-path clone failures in plugin install | `git config --system core.longpaths true`, or shorten `CLAUDE_CONFIG_DIR`. |

## Moving to a new VM
Run the installer; copy `~/.claude-hr/config.toml` if you have local overrides. Nothing else is machine-specific.
For a fleet, put shared overrides in `/etc/claude-hr/config.toml` (Windows `%ProgramData%\claude-hr\config.toml`).
