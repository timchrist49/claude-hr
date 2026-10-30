# Changelog

## 0.3.7
**Windows: claude-hr works where Windows refuses its launcher programs.** Fifth report from the PC that blocks `astral.sh` and
`claude.ai`, and now `claude-hr-ctl.exe` itself: Windows PowerShell stopped at the setup step with `Program 'claude-hr-ctl.exe'
failed to run: Access is denied`, and an Administrator PowerShell 7 with `... failed to run: StandardOutputEncoding is only
supported when standard output is redirected`. Those are one refusal: PowerShell 7 starts a program a second way when the first
fails, and that second start is what throws, which hides Windows' reason. The cause on that PC is unknown (ThreatLocker had been
removed; Defender attack-surface rules, AppLocker or an endpoint product are the usual suspects, and a refused new file next to
an allowed `uv.exe` and `python.exe` fits any of them). What claude-hr did wrong was to depend on files Windows may refuse.
- **`claude-hr-ctl launchers` (new) and a first `launchers` step in `setup`** (Windows). `uv tool install` writes `claude-hr.exe`,
  `claude-hr-ctl.exe` and `claude-hr-hook.exe`: new files, different on every PC. Each is started once with an argument that
  only prints something; one that Windows refuses (Windows error 5, 225, 1260, 1261 or 4551, believed only after three more
  looks over twelve seconds, since a scanner may still hold a fresh file) is moved aside as `<name>.exe.refused`, and two
  stand-ins are written next to it: `<name>.cmd` for PowerShell and cmd.exe and a shell script `<name>` for Git Bash (where the
  hooks and skills run). Both start the same entry point with `python -I -m claude_hr.<module>` (`-I`: `-m` would put the current
  directory first on the import path, and a launcher runs in whatever repository you are in). Every later look (setup, the post
  phase of an update, `launchers ensure`) probes again, so a launcher that starts again gets its name back and the stand-ins
  go, and a reinstall that writes a new refused launcher gets stand-ins again. Files that are not ours are never overwritten or
  deleted. `launchers.fallback = false` switches it off.
- **The cost is stated where it is incurred.** A `.cmd` hands its arguments to cmd.exe, which acts on `& | < >` and `% ^ !` in them
  and drops everything after a line break: never pass `claude-hr` text from an untrusted source that way (a script calls Python
  directly). `launchers.cmd = false` writes only the Git Bash scripts. The setup output, the installer's closing text, `doctor`
  and the docs all say so, and that this runs claude-hr through Python where the PC refuses the launcher programs (a policy of
  the organisation may be behind that: ask). Plain `claude` gets no such stand-in (`shim install` refuses): every IDE and script
  calls it with prompts.
- **Not covered, stated:** the end-of-turn gates of the `hr-gates` plugin start `claude-hr-hook` without a shell (exec form); whether
  Claude Code can start a `.cmd` that way was not verified, so on a PC with stand-ins assume the gates do not run.
- **Hardening (from the security reviews of this change):** the PowerShell tool is not let call a `.cmd` stand-in when anything
  after the call contains `& | < > ^ % !`, a line break (cmd.exe drops the rest of the arguments there), or a character with
  which PowerShell builds text at run time (`` ` $ @ ( ) { } ``;
  `Start-Process` is read whole, it takes arguments before the program). A permission rule or an approval prompt had judged
  the PowerShell text, and cmd.exe reads it again. The rule reads no PowerShell quoting on purpose: the first two versions did,
  and automated reviews found ways round each (backtick escapes, here-strings, a call inside `cmd /c "..."`). Strict (a pipe
  after the call is refused too); the message gives the Python command and the Bash tool, which have no such limit; best
  effort like the other guards, not a boundary. Stand-ins are only written by the Python of a `uv tool` environment
  (`uv-receipt.toml`): a development checkout or a venv under review never becomes claude-hr for every command and hook (a test
  run from a shell that exports `UV_TOOL_BIN_DIR` or `XDG_BIN_HOME` could not reach the real launchers either: the sandbox of the
  tests clears them, the guard that watches the real profile watches the stand-ins, and a relative value is ignored); a
  launcher that was already waited for is refused once, not four times
  (each refused start is an event for whoever watches the PC); the status line runs `"<python>" -I -m claude_hr.hooks statusline`
  by absolute path instead of the bare name `claude-hr-hook` (cmd.exe looks in the current folder first); a Python path with `!`
  gets no `.cmd`; `remove` takes the lock; the installer's evidence says to read it before pasting, and its copy-paste commands
  escape every single-quote character PowerShell knows.
- **Around it:** the status line runs the hook through Python when its launcher is replaced; Headroom is not downloaded where
  launchers are refused (`headroom.exe` comes from the same tool; the download is large) and a refused `headroom.exe` is
  reported instead of raising out of setup; `claude-hr-hook` runs with `python -m` (it had no `__main__` guard); `doctor`
  reports the state; `uninstall --purge` removes the stand-ins.
- **install.ps1:** the reason a program was refused now comes from Windows (a .NET start reports the Win32 error), so every
  PowerShell says `Access is denied (Windows error 5)`. A refused `claude-hr-ctl` no longer stops the install: setup runs through
  the tool's Python and the `launchers` step does the rest; if Python is refused too the install stops with the reason and both
  folders to allow. The installer prints what the PC recorded about the refused file (a Deny rule on it or its folder, security
  software that is present, a ThreatLocker that is still installed, and the Defender, code-integrity and AppLocker events of the
  last day that name it), tries `claude-hr-ctl` and `claude-hr` after setup, and ends with one of three texts. The running check
  also sees programs that run from the tool environment. The evidence is read even when Windows PowerShell inherits PowerShell 7's module path (it cannot load `Get-Acl` then; the CI runners hit it): the modules of the running PowerShell are loaded by their own path.
- **Tests:** real programs again (an execute-deny rule on a copy of `curl.exe` is the same "Access is denied"), in Windows
  PowerShell 5.1 and PowerShell 7; the stand-ins are run for real from PowerShell, cmd.exe and Git Bash, with arguments that
  contain spaces, `&`, `%` and `^`, with stdin, and with their exit code. `tests/windows/verify-windows-launchers.ps1` runs the
  real installer against a folder where every new file is refused (an inheritable deny rule) and lifts the block afterwards.

**Windows installer (`install.ps1`): found is not the same as working.** Second report from the same PC, after the fallbacks
below worked: `claude.ai` is blocked there too, winget installed Claude Code (a portable `claude.exe`, downloaded from
`downloads.claude.ai`, a different host), PowerShell found it on the PATH, and the last line of the installer, `claude --version`,
then died with `iex : Program 'claude.exe' failed to run: Access is denied` (Windows refused to start the program: security
software or application control on that PC). Three faults of the installer, none of them the cause of the block:
- A route counted as a success when the tool was on the PATH. Now the tool is started once (`--version`) after every route; one that
  will not start makes the route fail, and the next route is tried. A program that cannot be started is tried four times over
  15 s (a virus scanner may still hold a file written a moment ago); one that starts and fails is reported at once.
- When no route gives a working tool the message names the route, the path, Windows' own reason (without PowerShell's "At line"
  trace) and the command that shows it, and says that someone who manages the PC has to allow the file (and that a restart may
  be needed after a product such as ThreatLocker is removed).
- **A failing git or uv now says why.** Fourth report, from an Administrator PowerShell on that PC: `git could not fetch the
  claude-hr repository (check the URL and credentials)`. The installer had thrown git's output away and printed a guess, so
  nobody could see the cause (a first guess is git's "dubious ownership" check, which an elevated window can trigger on a
  folder another token created; not confirmed). The fetch, clone, checkout and `uv tool install` steps now show the last
  six lines the program printed, then a hint (for git: use a normal, not Administrator, window; git's message names the
  `git config --global --add safe.directory` command). Run the installer in a normal window: nothing in it needs Administrator.
- **claude-hr's own commands get the same check** (third report, after ThreatLocker had been removed and `claude.exe` ran:
  `iex : Program 'claude-hr-ctl.exe' failed to run: Access is denied` at the setup step). `claude-hr-ctl` is started once right
  after `uv tool install`; if Windows refuses it the install stops there with the path, the reason and the two folders to
  allow (`%USERPROFILE%\.local\bin`, `%APPDATA%\uv\tools`) instead of crashing in setup. What blocks it on that PC is not known.
- The last line crashed the script on any program that would not start. It is now a warning for a Claude Code that was there
  before (claude-hr installs anyway) and part of the install check for one that was just installed.
- Tests start real programs: a copy of `curl.exe` whose execute right is denied reproduces the exact error, and one that is denied
  at first and allowed on the second try stands for a scanner finishing. A Claude Code installed through winget is updated by
  winget, not by `claude-hr update` (stated in the README).

**Windows installer (`install.ps1`): a blocked download no longer ends the install.** The installer is fetched from `main`, so this
takes effect without a claude-hr release. Reported from a Windows PC where `irm .../install.ps1 | iex` stopped at the first
download with `Invoke-RestMethod : Unable to connect to the remote server` (PowerShell's message for a failed TCP connection to
`astral.sh`, so not TLS, DNS or a proxy login; most likely a firewall or web filter on that network) while GitHub, and so the
installer itself, was reachable.
- **uv has three routes**: Astral's installer, then uv's GitHub release (the zip is verified against the release's `.sha256`
  file before anything is unpacked, with .NET and not `Get-FileHash` / `Expand-Archive`: those are script functions that
  Windows PowerShell loads on first use, and on the CI runners that load failed; only `uv.exe`, `uvx.exe` and `uvw.exe` are
  taken out of the zip, by name; the folder is put on the PATH with `uv tool update-shell`, as Astral's installer does), then
  `winget install astral-sh.uv`. A route fails by throwing and the next one is tried; an installer that downloads but then fails
  while running (it could not fetch the program from its second host, `releases.astral.sh`) falls back too. **Downloaded
  installers run in a PowerShell of their own**: the real ones of uv and Claude Code end with `exit 1` when they fail, and
  run with `Invoke-Expression` (as `irm | iex` does) that `exit` closes the person's whole PowerShell window, error message
  and fallbacks included. Their output is shown, a failure keeps its exit code and last lines.
- **Claude Code has two**: the claude.ai installer, then `winget install Anthropic.ClaudeCode`.
- **When every route fails** the error lists each route with its reason (the URL is part of the message), then which of
  `astral.sh`, `releases.astral.sh`, `github.com`, `release-assets.githubusercontent.com`, `pypi.org`, `files.pythonhosted.org`
  and `claude.ai` this PC can reach (an error page counts as reachable), and how to install by hand.
- Downloads add TLS 1.2 where the .NET default lacks it (a system that decides for itself is left alone), wait at most 60 s and
  try twice; the progress bar is off (it makes Windows PowerShell 5.1 downloads many times slower). They are staged in a new
  folder under `%LOCALAPPDATA%\Temp` (not a `TEMP` that may be shared), checked there, run from there and deleted. The PATH of
  the running session is refreshed from the registry after each route instead of only gaining `~\.local\bin`. The signed-in
  user's credentials are deliberately not offered to a proxy: a rogue WPAD proxy could harvest them.
- **Tests** (`tests/test_installer_ps1.py`, Windows): the download block is cut out of `install.ps1` and run under the real
  PowerShell with the network, winget and uv replaced, so every route, the checksum refusal, an installer that ends with
  `exit 1`, the cleanup of the staging folder and each failure message are exercised; two tests use a real local server for the
  "error page is reachable, refused connection is not" rule. A security review of the first version found the proxy-credentials
  and shared-TEMP weaknesses (both removed); checking the real installers found the `exit` problem.

## 0.3.6
`claude-hr update` shows what it is doing, a run that ends early no longer locks out its own retry, and the background reinstall no
longer risks a half-deleted installation. From a Windows PC where `claude-hr update` moved the checkout to the new release and
then ended before launching the reinstall (cause not found: no crash, no stale lock, nothing in the event log): the installed tool
stayed on the old version, a retry answered "a reinstall was started in the last 15 minutes; waiting for it", and the command had
printed nothing for the whole minute, so it looked as if it had done nothing.
- **Progress lines.** At a terminal, `update` lists each component on stderr as it starts (`updating Claude Code (a new version
  takes about a minute) ...`). The report still comes at the end. The scheduled job and pipes get no progress lines.
- **The retry mark is written when the reinstall helper is launched, not when the checkout moves.** `state/self-update.json` made
  the next run wait 15 minutes for a reinstall; it was written before anything was launched, so a run that ended in between left a
  mark for a reinstall that never started. Now a run that ends early can be retried at once, a launch that fails or is interrupted
  (Ctrl-C, a closed console) clears the mark, and a reinstall that was launched still makes a second run wait. A mark that cannot
  be written (an antivirus scanner holding the file) no longer stops the launch. `logs/update.jsonl` gets a `self` row with action
  `launched` (and how: task scheduler, systemd unit or detached process) when the helper was started, so a missing launch shows.
- **The Windows helper does not reinstall over a tool environment that is in use.** It waits up to 2 minutes for the updater to
  exit and up to 2 more for every running `claude-hr` program. If one still runs from the tool environment when the wait ends (a
  session started right after `update`), the attempt used to go ahead, fail with `Access is denied` and could leave the
  environment half-deleted until a retry worked. It now logs `NOT reinstalling` and leaves claude-hr as it is; the checkout is
  already on the new release, so the next `claude-hr update` finishes the job once no claude-hr session runs (while one does, a
  plain `update` answers `deferred`). On Windows a reinstall that did not happen (this, or three failed attempts) clears the retry
  mark; on Linux and macOS a failed reinstall leaves it until its 15 minutes pass.
- **`update --force` under a running session says what will happen.** On Windows it queues the reinstall as before, but the helper
  skips it while a session runs; the report now says so (an `INFO:` line), and the `--force` help and the deferral hint no
  longer promise otherwise.
- **Tests.** Two updater tests and the helper tests no longer depend on whether a `claude-hr` session is running on the machine
  (they failed whenever one was); the sandboxed Windows self-update test waits long enough for a helper that has to wait for another
  session. New tests pin the order of the progress lines against the component runs, the mark on the Windows routes, the systemd
  route, a mark that cannot be written, an interrupted launch, and the helper's path comparison (case, a sibling folder) under
  real PowerShell.
- **Found by an independent review of this change** (fixed before release): a mark-write failure aborting the launch, the
  `--force` silence above, documentation that promised more than the code, and the test gaps in the previous bullet.
- **Documented** (OPERATIONS.md, Updates): the progress lines, "do not open `claude-hr` again until `claude-hr-ctl --version`
  shows the new version", how to tell whether the helper was launched, and the last-resort command.

## 0.3.5
The caps on Claude's background sessions now follow the machine as it grows, and `doctor` and the monitor show what the kernel really
gave them. From a Linux VM that froze twice while three background (`claude agents`) sessions ran an unbounded `jest` suite: the wrapper
that caps those sessions had been written for the VM's old size (7.6 GB RAM, so a 4 GB soft limit for the whole group) and nothing
re-checked it after the VM was resized, doctor printed a freshly computed per-session number instead of the cap the running scope
had, and the monitor (every 15 minutes) looked at RAM, disk, `~/.claude` and the proxy, not at the scope. The exact stall mechanism
could not be confirmed (the kernel logs were gone); the stale cap, the shared scope and the blind spots could, from the code and the
machine's own files.
- **The wrapper is kept right for the machine.** It records what it was sized for (`# sized for:` line), and the scheduled monitor
  re-writes it when RAM, CPUs or `resources.*` change by more than 5% (`monitor.heal_resource_caps`, on by default; `doctor` shows
  `agent caps` with both sizes while it is stale). A wrapper from before 0.3.5 whose limits are still right is re-written once to add
  the record, without a warning.
- **The scope that is already running is raised too.** The background service keeps its scope, and its caps, for days, so a new wrapper
  alone changes nothing. The monitor raises a running `claude-hr-*.scope` with `systemctl --user set-property --runtime` (verified on
  systemd 249): never given a cap it did not have, no restart, and a soft limit the config no longer wants (`memory_high_gb = 0`) is
  lifted. A LOWER number (the VM shrank) is not applied to a running scope: `doctor` flags it as larger than the machine allows and
  `claude-hr-ctl daemon restart` replaces it once no session is running.
- **`doctor` reads the running scope** (`agent scopes`): what it holds without reclaimable page cache and with it, hard cap and soft
  limit, tasks, how often it went over the soft limit and reached the hard cap, with a warning near the ceiling or when the scope does
  not match this machine. `agent caps` is only shown when the caps can really be applied (a systemd user bus). It also says when
  `resources.cpu_quota_pct` is set but a running scope has no CPU limit, and why: the cpu controller is not delegated to the user
  manager (systemd accepts `CPUQuota` and does nothing), or the scope was started before the setting.
- **The monitor watches the scope and the kernel's pressure stall information.** New alerts `scope-full` (the working set, not
  `memory.current`: a long-lived scope keeps clean page cache up to its soft limit, which a test showed reads as "full" with pressure
  0.00), `scope-capped` (reached the hard cap), `scope-oom` (lost a process to the OOM killer; both counted since the previous tick,
  including everything a scope that appeared in between has counted so far) and `pressure` (memory or I/O stalled tasks for 20% of the
  time since the previous tick, from the cumulative stall counters, so a stall in the middle of the 15 minutes is not missed;
  thresholds `monitor.scope_warn_pct` and `monitor.psi_warn_pct`). A damaged `state/health.json` no longer stops the monitor.
- **The wrapper now honours the config it ignored:** `resources.cpu_quota_pct` (per session, times the up to four the shared scope
  holds, at most all cores) and `resources.memory_high_gb = 0` (no soft limit: the kernel kills at the hard cap instead of
  throttling the allocating tasks above it).
- **Not changed, on purpose:** the soft limit stays at 75% of the hard cap. It was an unexplained default since 0.1.0; measured now
  on real cgroups (a 2 GB scope, swap off, 3 sessions of 350 MB timing small allocations, 15 workers growing towards 3.3 GB): at 75%
  the workers were slowed to a crawl, nothing was killed and the sessions' allocations took 2 ms at most; at 90% the workers broke
  through to the hard cap after 16 s and the OOM killer killed a session twice (it takes the largest process, not the runaway); with
  no soft limit it killed a session after 1.5 s. A narrower band, or `memory_high_gb = 0`, trades a slow test run for a dead session.
  The test was anonymous memory only, so it does not explain the VM freeze; that cause is still open. Also unchanged:
  sessions hosted by the background service are still not counted by admission control (they are bounded by their shared scope; the
  docs now say so); the monitor still runs every `monitor.interval_minutes` (15; `schedule install` applies a changed value), so an
  alert follows an event rather than predicting it; and sessions do not get a scope each (a scope created inside another is a sibling
  and would escape the shared cap). Bounding the test runner remains the real prevention: `jest --maxWorkers=2`, `pytest -n 2`,
  `cargo test -- --test-threads=2`.

## 0.3.4
The managed CLAUDE.md block now nudges security-sensitive changes to the security reviewer that `hr-core` already ships.
- **One new baseline line** (Security section): if the change touches authentication, file paths built from untrusted input,
  subprocesses, SQL, deserialization, secrets or crypto, call `hr-core:security-reviewer` before the final answer and fix what
  it confirms; a trivial edit, docs or a test-only change needs none of it; it applies only if that agent is listed. The reviewer
  is read-only and pinned to Opus, so a trigger costs more on that step. Run `claude-hr update` to re-apply the baseline.
- **Measured** (headless, Sonnet at medium effort, own login, rule delivered with `--append-system-prompt`): on a path-traversal
  hardening task the reviewer ran in 3 of 3 runs with the rule and in 0 of 1 without it (run cost $0.22-0.24 with the review
  against $0.14 without; all runs passed the hidden checks, 18 of 18). On controls it ran in 0 of 4 runs (a trivial fix twice, a
  feature task twice). Small n, one task of this kind, Sonnet medium rather than a long Opus session: a rule is a nudge, not a
  guarantee. A rule of the same kind for a planner and a code reviewer (vendored ECC agents, an earlier experiment) routed
  poorly (planner 0 of 8 qualifying runs, code reviewer about 1 of 8), so only the security line ships and no ECC code is included.

## 0.3.3
No token cap by default, and ECC, Ponytail and Chisle were tested head to head instead of judged on paper.
- **`footprint.budget_standard_tokens` is now 0 = no cap.** It was 1500 and made `doctor` warn and `footprint --strict` fail above
  it (standard profile only). A cap limits what the setup can do without making the model any smarter, so `footprint` and
  `doctor` now measure and show the always-on estimate for every profile and refuse nothing. A number above 0 still turns the
  warning and `--strict` on. docs/ADDONS.md admission rule 5 says the same: measure the cost and justify it, no fixed cap.
- **Measured, not estimated** (docs/ADDONS.md): each add-on loaded for one headless session with `--plugin-dir`, nothing
  installed, own login, Sonnet at medium effort, five small Python tasks with hidden checks (47 runs, about $4.60 in total).
  ECC adds 13,635 tokens to the first request, doubles the cost of a run ($0.137 against $0.069) and adds about 13 s, was never
  asked to invoke a skill or agent, and its hooks write a log of every Bash command into `~/.claude`; hidden checks passed
  9 of 9 against 8 of 9 without it, and the one miss did not recur in four repeats. Ponytail adds 1,591 tokens, cuts output by
  about 27% and costs the same per run. Chisle adds 1,920 tokens, cuts output by about 21% and costs 5% more per run; its tool-output
  trimming removed the decisive line from the middle of a long log in 3 of 3 runs, and the model found it again only because it
  noticed the marker. None of the three is enabled by default. Small samples: they say what the extra listing bought on small
  tasks, not what it would do on a long planning task.

## 0.3.2
Cost visibility: the plan meter is shown as cash, and the usage ledger's prices are corrected. From an audit of where tokens and
money go: a full 7-day window quietly turns further use into extra-usage charges, and nothing here said so.
- **`status`, `doctor` and the status line show the plan meter.** Once the 7-day window is full, extra usage is billed at API
  prices. Headroom already polled the meter; nothing showed it. `claude-hr-ctl status` now prints the extra-usage dollars and a
  warning when the window is full and extra usage is on (or at 80% of its monthly limit); `doctor` has a `plan meter` check
  (WARN in those cases, INFO when the window is nearly full); the status line adds `7d` from 80% and the extra dollars when the
  window is full. No network: it reads the poll Headroom keeps in `headroom/subscription_state.json`, and a poll older than 30
  minutes is shown with its time.
- **`usage` prices are corrected.** Sonnet 5.5 cache hits were priced at $0.20 per million tokens; the pricing page lists $0.10
  (0.05x the input price, as for Opus 5.5), which inflated the weekly estimate by about 28%. Fast mode was ignored: a message
  whose usage says speed `fast` on Opus 5.5, 5 or 4.8 now costs twice the price in every class. The report footnote says what is
  and is not modelled.
- **The `~/.claude is large` alert is honest.** It says "at least N GB" when the size scan ran out of time, and that finished
  workflow scratch under `~/.claude/jobs` is only removed after `maintenance.jobs_retention_days`, so `cleanup` may free little
  today (it used to ask for a cleanup that could reclaim nothing).
- **The managed CLAUDE.md gets one line:** before a workflow or a fan-out of more than ~10 agents, check the plan meter; with the
  7-day window above 90% or extra usage being spent, use fewer agents, Sonnet for mechanical stages, or defer.

## 0.3.1
The git guard now asks about more ways of skipping hooks, and Everything Claude Code (ECC) was reviewed and rejected.
- **The bash guard reads git commands as words, and keeps the old pattern as a backstop.** It now asks before `git commit -n`
  (also inside a cluster such as `-an`), abbreviations of `--no-verify` from `--no-veri` on, and `git -c core.hooksPath=` (also
  `--config-env`), and it follows `$(...)`, backticks, `sh -c '...'` and `eval`, line continuations, `if`/`then`/`exec`/`env`
  in front of git, and quoted separators. It no longer asks when a quoted commit message only mentions `--no-verify`. The old
  pattern still runs (with quoted `-m` / `--message` values cut out), so nothing it asked about before is lost except such
  messages. Measured on the test commands (tests/test_hooks.py): of 51 that skip or redirect hooks the old pattern asked on 9
  and the new guard asks on all 51; of 25 harmless ones (commit messages that mention the flag, `git push -n`, `--no-verbose`...)
  the old pattern asked wrongly on 6 and the new guard on none. The decision is still an ask (a refusal under
  `security.profile = "strict"`), an error falls back to the old pattern, and no hook, process or token is added. The PowerShell
  tool, commands over 64 KiB and unbalanced quotes use the old pattern alone (documented in SECURITY.md). The test inputs are
  short command strings taken from ECC's `block-no-verify` tests (MIT, credited in tests/test_hooks.py); no ECC code is copied.
- **`plugins audit` flags ECC** (`ecc`, `everything-claude-code`) if it is installed, and a test keeps it out of every profile
  and the marketplace.
- **Docs:** the ECC review in `docs/ADDONS.md` (what was found, which figures are measured and which inferred, why it was not
  adopted, when to revisit) and a vetting checklist for third-party code (admission rule 6); the e2e scripts check the new
  guard cases.

## 0.3.0
`claude-hr update` now updates everything, and the token ledger sees the subagent work that is most of the volume.
- **`claude-hr update` (and `claude-hr-ctl update`, and the daily job) updates the whole setup:** claude-hr itself, Claude Code,
  Headroom, user-scope plugins and their marketplaces, then re-applies the settings baseline and CLAUDE.md block, restarts the
  Headroom proxy only when no session runs and it was started with older settings, and prints one report (component, before ->
  after, result; `Everything is up to date.` or what is pending/failed and why) with a health verdict. Until 0.2.3 `claude-hr update`
  only ran Claude Code's own `claude update`; that is now `claude-hr update --claude-only`. Flags: `--check` (a preview of current ->
  target per component, still runs `git fetch`; plugins cannot be previewed), `--only` (now also `baseline`, `proxy`; always the
  fixed order), `--no-restart`, `--force`, `--allow-downgrade`. Exit codes: 0 ok, 1 a component failed or could not be checked,
  2 usage error, 75 deferred because sessions run or another update is running (the scheduled job counts 75 as success). A
  cross-process lock (`state/update.lock`) keeps two updates from running at once.
- **The part that needs the new code runs after claude-hr itself was reinstalled:** the detached helper (Windows scheduled task
  through `conhost --headless`, systemd unit, detached spawn) chains `python -I -m claude_hr.ctl update --post` in the state folder
  after a reinstall that exited 0; the result is in `state/update-last.json` and `claude-hr-ctl status`. The session guard is asked
  again right before the reinstall. Under systemd a stale proxy is stopped and the monitor job or the next launch starts it (a proxy
  started inside the transient unit would die with it).
- **Claude Code was frozen on this kind of machine:** `claude.channel` defaulted to `stable` while the user's own
  `autoUpdatesChannel` said `latest`, and the built-in auto-updater is off, so nothing ever moved. `claude.channel` is now `auto`
  (the user's `autoUpdatesChannel` when it is stable/latest, else stable); the report shows the channel in force and what it offers
  instead of a bare "not downgrading"; `doctor` shows it and warns when an explicit setting disagrees.
- **`doctor`/`update` say it when Claude Code runs outside claude-hr:** plain `claude` sessions are not routed through Headroom and
  not counted by the session guard (an INFO line says how many, and whether they use the proxy through `ANTHROPIC_BASE_URL`, in
  which case they also block a proxy restart or a Headroom upgrade).
- **With the `claude` shim,** `claude update` typed in a terminal means update-everything; scripts, IDEs and CI still get Claude's own
  updater. The POSIX shim script now passes `--hr-shim`: run `claude-hr-ctl shim install` again to refresh an old shim.
- **`usage` counts subagent and workflow transcripts.** The ledger, `usage --tools`, `plugins audit` and `status` read only
  `projects/*/*.jsonl` before; on the author's machine subagents are about 67% of the prompt volume and 78% of the output, and the
  numbers were understated about three times. The report adds an estimated API list-price cost (`est.cost`, `n/a` for an unpriced model;
  tiered/long-context/batch pricing ignored; an estimate, not a bill), a main-vs-subagent split and a note when subagents are more
  than half. `--no-subagents` gives the old numbers. Messages are de-duplicated by (message id, request id) across files; scans are
  capped (120 s, `status` 15 s) and say so when cut short; unreadable files are counted; on Windows long transcript paths are opened in
  the extended-length form. The weekly snapshot stores seven whole days from complete scans only, labelled `main+subagents`.
- **The credentials scan finds what it missed:** `doctor` said "MCP literal secrets: none" while `~/.claude.json` held plain-text
  (temporary) AWS credentials in a project-scoped MCP server. It now scans project-scoped servers, settings files and project
  `.mcp.json` files (regular files only, bounded, per-file error isolation), recognises passwords in connection strings and opaque
  Authorization headers, ignores references and file paths, sanitises every printed name, and says what a cap left unscanned.
  `creds scan` reads main sessions before subagent transcripts so the 400 MB budget covers the sessions that matter.
- **Review:** an adversarial review (5 reviewers, a reproducer and a refuter per finding) confirmed 49 issues; all that mattered are
  fixed, among them `--no-restart` being ignored by the Headroom upgrade, a stale proxy record that could make the updater stop a
  proxy it did not start, an exception during the Headroom upgrade leaving the proxy down, `python -m claude_hr.ctl` importing from the
  user's current folder, untrusted project files read without limits, and report text that said "up to date" next to a failure.
- **Not done, on purpose:** Ponytail and Chisle stay off (Ponytail is vendored and opt-in; Chisle is not in the project: its terse,
  "lazy first" rules conflict with ultracode and its output trimming can drop errors); the Serena pin changes only with a claude-hr
  release; uv, git and Python are not updated by anything.

## 0.2.3
- **Fixed: false "Headroom proxy down" alarms.** A proxy nobody has asked for a while answers its first request slowly (0.4-0.8 s
  measured on Windows, the first `/health` after 15 idle minutes), while the quick probe gives up after 0.6 s; the next request
  takes a millisecond. The monitor job (every 15 minutes) therefore raised "Headroom proxy down" and "proxy restart requested" at
  most of its ticks for a proxy that was running fine, `doctor` and `status` could print "not serving" right after a quiet spell,
  and the launcher printed "waiting for the Headroom proxy (up to 90s ...)" for a healthy one. New `Headroom.health_patient()`:
  when something is listening but the quick probe got no answer, it asks once more with a longer timeout (nothing listening is
  still "down" at once). Used by the monitor (check and restart decision), `doctor`, `status`, `headroom status`, the updater's
  "was it running" check and, with a short 1.5 s patience, the launcher.

## 0.2.2
- **Fixed: an empty terminal window titled with the path of `headroom.exe` kept popping up on Windows, and closing it killed the
  Headroom proxy.** Every proxy start (the launcher's cold start, and the monitor job restarting a dead proxy) was made with
  `DETACHED_PROCESS`, which overrides `CREATE_NO_WINDOW`: uv's `headroom.exe` trampoline then had no console, and the
  `python.exe` it starts got a new visible one (a window, or a Windows Terminal tab). Closing that tab ended the proxy (exit code
  `0xC000013A`); the monitor restarted it 15 minutes later and the window came back. The proxy is now started with
  `CREATE_NO_WINDOW` only, so it owns a hidden console that its own child inherits and no window can appear. The self-update
  helper's fallback start had the same flag and got the same fix. One shared definition, `procutil.windows_background_flags()`.
- **Tests:** unit tests for the flags of both spawn sites, a real-window test (starts a console program from a console program
  with the flags claude-hr uses and looks at the desktop session's windows; `CLAUDE_HR_TEST_WINDOW_CONTROL=1` adds the control
  that the old flags do open one), and `tests/windows/proxy_window_probe.py`, which starts the real proxy through
  `Headroom.start()` and reports any new console window.
- **The self-update helper and the monitor's alert command no longer open a window either.** The helper's one-shot scheduled
  task ran `powershell.exe` directly; Task Scheduler gives it no console, so Windows Terminal opened a window that stayed
  (minimised) for the whole reinstall, and closing it killed the reinstall. The task now runs it through `conhost.exe
  --headless` (checked once that this Windows supports it, else as before). `monitor.alert_command` is started with
  `CREATE_NO_WINDOW` and no stdin (the monitor job runs under `pythonw.exe`).
- **Fixed: `pytest tests` failed on Linux and macOS with a collection error.** 0.2.1 added a Windows-only script
  (`tests/windows/console_offer_test.py`) whose name pytest collects and which uses `ctypes.WinDLL` at import time. It is
  now `tests/windows/offer_console_check.py`, and `tests/conftest.py` keeps pytest out of `tests/windows/*.py` altogether.
- If you are on 0.2.1 and see the window: update (`claude-hr-ctl update`, or `install.ps1` again) and, once no Claude session is
  running, `claude-hr-ctl headroom restart`.

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
