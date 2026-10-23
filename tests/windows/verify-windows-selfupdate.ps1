# Windows regression test for the self-update path (Windows PowerShell 5.1 or 7):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\verify-windows-selfupdate.ps1 [-FromRef v0.1.2]
# Builds a throwaway git origin with releases v9.0.1 -> v9.0.2 -> v9.0.3 from this checkout (or from -FromRef, to test an
# older updater), installs v9.0.1 into an isolated sandbox, then runs the update with `--only self plugins` (the order of
# the daily job) twice: round 1 through the LAUNCHER (`claude-hr update`), round 2 through `claude-hr-ctl update`. After each
# run the tool environment must still work: the detached reinstall must not overlap with the plugin update (it used to leave
# claude-hr without site-packages: "ModuleNotFoundError: No module named 'claude_hr'"), and the post phase chained behind the
# reinstall (`claude-hr-ctl update --post`, which needs the NEW code) must have run: state\update-last.json is complete and
# was written by the new release. That helper is a scheduled task, which does not inherit this sandbox: it must work on the
# sandbox profile (never the real one), so a record in the sandbox is also the proof that it did.
# With -FromRef the installed updater is an OLDER one (no `claude-hr update`, no post phase): round 1 then uses
# `claude-hr-ctl update` and the post-phase checks are skipped.
# No Headroom download, no scheduled jobs, no model calls; ~3 minutes. Never writes to your real ~/.claude or ~/.claude-hr.
param([string]$FromRef = '')
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$sb = Join-Path $env:TEMP 'esu1'
if (Test-Path $sb) { Remove-Item -Recurse -Force $sb }
New-Item -ItemType Directory -Force -Path "$sb\h\.claude", "$sb\tooldir", "$sb\toolbin", "$sb\src" | Out-Null
$realConfig = "$env:USERPROFILE\.claude"
$tasksBefore = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
function RealProfileSnapshot { (@("$env:USERPROFILE\.claude\settings.json", "$env:USERPROFILE\.claude-hr\config.toml", "$env:USERPROFILE\.claude-hr\state\verify-trust.json", "$env:USERPROFILE\.claude-hr\state\verify-offer.json") | ForEach-Object { if (Test-Path $_) { "$_=" + (Get-FileHash $_ -Algorithm SHA256).Hash } else { "$_=absent" } }) -join ';' }
$realBefore = RealProfileSnapshot  # a machine may already run claude-hr: compare before and after, not 'absent'
$pass = 0; $fail = 0
function Check($name, [bool]$ok, $detail = '') { if ($ok) { $script:pass++; "  PASS $name" } else { $script:fail++; "  FAIL $name  $detail" } }
$utf8 = New-Object Text.UTF8Encoding $false
function Run($cmdline) { (cmd /c "$cmdline 2>&1") | Out-String }
function Sub-InFile($path, $pattern, $replacement) {
  $t = [IO.File]::ReadAllText($path); [IO.File]::WriteAllText($path, ([regex]::Replace($t, $pattern, $replacement)), $utf8)
}
function Set-Release($dir, $v) {
  Sub-InFile "$dir\src\claude_hr\__init__.py" '__version__ = "[^"]+"' "__version__ = `"$v`""
  Sub-InFile "$dir\pyproject.toml" '(?m)^version = "[^"]+"' "version = `"$v`""
  Sub-InFile "$dir\plugins\hr-core\.claude-plugin\plugin.json" '"version": "[^"]+"' "`"version`": `"$v`""
  Sub-InFile "$dir\.claude-plugin\marketplace.json" '"version": "0\.[^"]+"|"version": "9\.[^"]+"' "`"version`": `"$v`""
}

# The updater's helper runs from Task Scheduler, which does not inherit this sandbox's environment: if it ever installed into
# the real uv tool directory we want to see it. Record the real state before isolating.
function RealUvState {
  $t = $env:UV_TOOL_DIR; $b = $env:UV_TOOL_BIN_DIR
  Remove-Item Env:UV_TOOL_DIR, Env:UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue
  try { ((cmd /c "uv tool list 2>&1") -join "`n") + "`n" + ((Get-ChildItem "$env:USERPROFILE\.local\bin" -Filter 'claude-hr*' -ErrorAction SilentlyContinue | ForEach-Object Name) -join ',') }
  finally { $env:UV_TOOL_DIR = $t; $env:UV_TOOL_BIN_DIR = $b }
}
$realUvBefore = RealUvState

# isolation: nothing below may write to the real profile
$env:CLAUDE_HR_USER_HOME = "$sb\h"; $env:CLAUDE_CONFIG_DIR = "$sb\h\.claude"; $env:CLAUDE_HR_HOME = "$sb\h\.claude-hr"
$env:UV_TOOL_DIR = "$sb\tooldir"; $env:UV_TOOL_BIN_DIR = "$sb\toolbin"
$env:CLAUDE_HR_HEADROOM__PORT = '18789'
$env:HF_HOME = "$sb\hf"
"PSVersion $($PSVersionTable.PSVersion)"

"== 1. throwaway origin with v9.0.1 / v9.0.2 =="
$src = "$sb\src"
if ($FromRef) {
  git -C $repo archive --format=zip $FromRef -o "$sb\src.zip" 2>&1 | Out-Null
  Expand-Archive -Path "$sb\src.zip" -DestinationPath $src -Force
} else {
  robocopy $repo $src /E /XD .git .venv __pycache__ .pytest_cache .ruff_cache /XF .git /NFL /NDL /NJH /NJS /NP | Out-Null  # /XF .git: in a git worktree .git is a FILE; copied, it made the git commands below commit into the real repository
}
Check "source tree copied" (Test-Path "$src\src\claude_hr\updater.py") "from '$FromRef'"
git -C $src init -q -b main 2>&1 | Out-Null
git -C $src config user.email t@example.com; git -C $src config user.name t
git -C $src config core.autocrlf false
Set-Release $src '9.0.1'
git -C $src add -A 2>&1 | Out-Null; git -C $src commit -q -m "release 9.0.1" 2>&1 | Out-Null; git -C $src tag v9.0.1
New-Item -ItemType Directory -Force -Path "$src\plugins\hr-core\skills\hr-selftest" | Out-Null
[IO.File]::WriteAllText("$src\plugins\hr-core\skills\hr-selftest\SKILL.md", "---`nname: hr-selftest`ndescription: Test skill added by the self-update regression test.`ndisable-model-invocation: true`n---`nNothing to do.`n", $utf8)
Set-Release $src '9.0.2'
git -C $src add -A 2>&1 | Out-Null; git -C $src commit -q -m "release 9.0.2" 2>&1 | Out-Null; git -C $src tag v9.0.2
git clone -q --bare $src "$sb\origin.git" 2>&1 | Out-Null
Check "origin has both release tags" ((git -C "$sb\origin.git" tag --list 'v9.*' | Out-String) -match 'v9\.0\.2')

"== 2. install v9.0.1 from the origin (real install.ps1) =="
Set-Location $repo
$t0 = Get-Date
$log = & "$repo\install.ps1" -Repo "$sb\origin.git" -Ref v9.0.1 -Profile minimal -NoHeadroomInstall -SkipSchedule *>&1 | Out-String
"install took {0:N0}s" -f ((Get-Date) - $t0).TotalSeconds
Check "install.ps1 completed" ($log -match 'claude-hr is installed') (($log -split "`n" | Select-Object -Last 8) -join ' | ')
$env:PATH = "$sb\toolbin;$env:PATH"
$st = Run "claude-hr-ctl status"
Check "installed version is 9.0.1" ($st -match 'claude-hr 9\.0\.1') $st
Check "profile minimal was saved" ((Run "claude-hr-ctl config get plugins.profile") -match 'minimal')

function Wait-ForVersion($v, $seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    $o = Run "claude-hr-ctl status"
    if ($o -match "claude-hr $([regex]::Escape($v))") { return $o }
    Start-Sleep -Seconds 3
  }
  return $o
}
# The post phase runs in the helper after the reinstall: wait for it to finish the record (phase 'complete', written by $v)
function Wait-ForLastUpdate($v, $seconds) {
  $f = "$sb\h\.claude-hr\state\update-last.json"
  $deadline = (Get-Date).AddSeconds($seconds)
  $rec = $null
  while ((Get-Date) -lt $deadline) {
    try { $rec = Get-Content $f -Raw -ErrorAction Stop | ConvertFrom-Json } catch { $rec = $null }
    if ($rec -and $rec.phase -eq 'complete' -and $rec.versions.'claude-hr' -eq $v) { return $rec }
    Start-Sleep -Seconds 3
  }
  return $rec
}
function Verify-PostPhase($label, $v) {
  $rec = Wait-ForLastUpdate $v 270
  Check "$label : the post phase ran after the reinstall (state\update-last.json is complete and written by $v)" ($rec -and $rec.phase -eq 'complete' -and $rec.versions.'claude-hr' -eq $v) (($rec | ConvertTo-Json -Compress -Depth 4))
  Check "$label : the record names the self update and the plugins step" ((@($rec.results | Where-Object { $_.component -eq 'self' -and $_.action -eq 'updated' }).Count -eq 1) -and (@($rec.results | Where-Object { $_.component -eq 'plugins' }).Count -eq 1)) (($rec.results | ConvertTo-Json -Compress -Depth 4))
  $helperLog = Get-Content "$sb\h\.claude-hr\logs\self-update.log" -Raw -ErrorAction SilentlyContinue
  Check "$label : the helper log shows the reinstall succeeded and the post phase exited 0" (($helperLog -match 'reinstall exit code 0') -and ($helperLog -match 'post phase exit code 0')) $helperLog
  Check "$label : the post phase ran isolated (python -I, so nothing from the directory update was typed in is imported)" ($helperLog -match 'running the post phase: .* -I -m claude_hr\.ctl update --post') $helperLog
  $st = Run "claude-hr-ctl status"
  Check "$label : claude-hr-ctl status shows the last update" ($st -match 'Last update: ') $st
}
function Verify-Update($label, $v) {
  $o = Wait-ForVersion $v 270
  if ($o -notmatch "claude-hr $([regex]::Escape($v))") { "  --- helper log:"; Get-Content "$sb\h\.claude-hr\logs\self-update.log" -ErrorAction SilentlyContinue | ForEach-Object { "      $_" } }
  Check "$label : claude-hr-ctl runs on $v (no ModuleNotFoundError)" (($o -match "claude-hr $([regex]::Escape($v))") -and ($o -notmatch 'ModuleNotFoundError')) ($o.Substring(0, [Math]::Min(300, $o.Length)))
  $tl = Run "uv tool list"
  Check "$label : uv tool environment is intact" ($tl -match "claude-hr v$([regex]::Escape($v))") $tl
  '{"tool_input":{"command":"cat .env"}}' | claude-hr-hook bash-guard 2>&1 | Out-Null
  Check "$label : claude-hr-hook still works (exit 2 on cat .env)" ($LASTEXITCODE -eq 2)
  $pl = Run "claude plugin list --json"
  $hrcore = $null; try { $hrcore = @($pl | ConvertFrom-Json | Where-Object { $_.id -like 'hr-core@*' })[0] } catch {}
  Check "$label : hr-core plugin updated to $v" ($hrcore -and ($hrcore.version -eq $v)) $pl
}

"== 3. update --only self plugins (the daily job's order) =="
# the helper is a task started by Task Scheduler: it must not open a console window (it used to open a Windows Terminal one)
$watchOut = Join-Path $sb 'windows-seen.json'
$watcher = Start-Process -FilePath python -ArgumentList "`"$repo\tests\windows\window_watch.py`"", 100, "`"$watchOut`"" -WindowStyle Hidden -PassThru
$u1 = if ($FromRef) { Run "claude-hr-ctl update --only self plugins" } else { Run "claude-hr update --only self plugins" }
$u1rc = $LASTEXITCODE
$u1.TrimEnd()
Check "the update command exited 0" ($u1rc -eq 0) "exit code $u1rc"
Check "update reported self updated" ($u1 -match '(?m)^self\s.*updated') $u1
if (-not $FromRef) { Check "update said that the rest finishes in the background" ($u1 -match 'claude-hr itself is updating in the background; the rest finishes there') $u1 }
Verify-Update 'round 1' '9.0.2'
if (-not $FromRef) { Verify-PostPhase 'round 1' '9.0.2' }
$watcher | Wait-Process -Timeout 130 -ErrorAction SilentlyContinue
$seen = $null; try { $seen = Get-Content $watchOut -Raw | ConvertFrom-Json } catch {}
Check "the helper task opened no console window (a Windows Terminal one used to stay open for the whole reinstall)" ($seen -and (@($seen.opened_and_closed).Count -eq 0)) (($seen | ConvertTo-Json -Compress))

"== 4. a second release, same sequence =="
Set-Release $src '9.0.3'
git -C $src add -A 2>&1 | Out-Null; git -C $src commit -q -m "release 9.0.3" 2>&1 | Out-Null; git -C $src tag v9.0.3
git -C $src push -q "$sb\origin.git" main --tags 2>&1 | Out-Null
$u2 = Run "claude-hr-ctl update --only self plugins"
$u2.TrimEnd()
Check "update reported self updated again" ($u2 -match '(?m)^self\s.*updated') $u2
Verify-Update 'round 2' '9.0.3'
if (-not $FromRef) { Verify-PostPhase 'round 2' '9.0.3' }
# the helper's last two lines (delete its script, unregister its task) run just after the reinstall that Verify-Update waited for
$cleaned = $false
foreach ($i in 1..15) {
  $cleaned = (-not (Get-ScheduledTask -TaskPath '\claude-hr\' -TaskName 'selfupdate' -ErrorAction SilentlyContinue)) -and (-not (Test-Path "$sb\h\.claude-hr\state\self-update.ps1"))
  if ($cleaned) { break }
  Start-Sleep -Seconds 2
}
Check "the one-shot helper task removed itself and its script" $cleaned ((Get-Content "$sb\h\.claude-hr\logs\self-update.log" -Tail 6 -ErrorAction SilentlyContinue) -join ' | ')
Check "nothing left to update" ((Run "claude-hr-ctl update --check --only self") -notmatch 'available')

"== 5. uninstall + real profile untouched =="
$un = Run "claude-hr-ctl uninstall"
Check "uninstall does not claim the scheduler failed" ($un -notmatch 'schedule: FAILED') $un
Check "the real ~/.claude/settings.json and ~/.claude-hr (config, trust, offer records) are unchanged by this run" ((RealProfileSnapshot) -eq $realBefore)
$tasksAfter = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
Check "scheduler tasks unchanged (none left behind, none of yours removed)" ($tasksAfter -eq $tasksBefore) "before: $tasksBefore after: $tasksAfter"
$realUvAfter = RealUvState
Check "your real uv tools and ~/.local/bin are untouched" ($realUvAfter -eq $realUvBefore) "before: $realUvBefore | after: $realUvAfter"
"SELFUPDATE_WIN_RESULT: $pass passed, $fail failed"

Set-Location 'C:\'
Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { taskkill /PID $_.OwningProcess /T /F | Out-Null }
Remove-Item -Recurse -Force $sb -ErrorAction SilentlyContinue
"sandbox removed: $(-not (Test-Path $sb))"
"WIN_DONE"
