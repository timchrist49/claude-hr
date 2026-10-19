# Windows end-to-end verification of claude-hr from a clean, isolated sandbox (Windows PowerShell 5.1 or 7):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\verify-windows.ps1
# Installs into %TEMP%\esb3 (own uv tool dir, Claude config dir and claude-hr home), runs 45 checks incl. the real Headroom
# install and TWO tiny `claude -p` calls (haiku) with your real login, then uninstalls and deletes the sandbox. It never writes
# to your real ~/.claude or ~/.claude-hr, and it verifies that afterwards. Takes ~3-5 minutes (about 0.5 GB download, 2 GB on disk).
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$sb = Join-Path $env:TEMP 'esb3'
if (Test-Path $sb) { Remove-Item -Recurse -Force $sb }
New-Item -ItemType Directory -Force -Path "$sb\h\.claude", "$sb\tooldir", "$sb\toolbin", "$sb\proj" | Out-Null
$realConfig = "$env:USERPROFILE\.claude"
$tasksBefore = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
function RealProfileSnapshot { (@("$env:USERPROFILE\.claude\settings.json", "$env:USERPROFILE\.claude-hr\config.toml", "$env:USERPROFILE\.claude-hr\state\verify-trust.json", "$env:USERPROFILE\.claude-hr\state\verify-offer.json") | ForEach-Object { if (Test-Path $_) { "$_=" + (Get-FileHash $_ -Algorithm SHA256).Hash } else { "$_=absent" } }) -join ';' }
$realBefore = RealProfileSnapshot  # a machine may already run claude-hr: compare before and after, not 'absent'
$pass = 0; $fail = 0
function Check($name, [bool]$ok, $detail = '') { if ($ok) { $script:pass++; "  PASS $name" } else { $script:fail++; "  FAIL $name  $detail" } }

# isolation: nothing below may write to the real profile
$env:CLAUDE_HR_USER_HOME = "$sb\h"; $env:CLAUDE_CONFIG_DIR = "$sb\h\.claude"; $env:CLAUDE_HR_HOME = "$sb\h\.claude-hr"
$env:UV_TOOL_DIR = "$sb\tooldir"; $env:UV_TOOL_BIN_DIR = "$sb\toolbin"
$env:LOCALAPPDATA_REAL = $env:LOCALAPPDATA
$env:CLAUDE_HR_HEADROOM__PORT = '18788'
$env:HF_HOME = "$sb\hf"
"PSVersion $($PSVersionTable.PSVersion)"

"== 1. full install via install.ps1 (real Headroom [all] install, proxy start, MCP, plugins) =="
Set-Location $repo
$t0 = Get-Date
# *>&1: install.ps1 reports its final status with Write-Host, which `2>&1` does not capture
$log = & "$repo\install.ps1" -Local . -SkipSchedule *>&1 | Out-String
"install took {0:N0}s" -f ((Get-Date) - $t0).TotalSeconds
$log -split "`n" | Where-Object { $_ -match '\[ok \]|\[ERR\]|ERROR|installed 0|pinned|Done' } | Select-Object -First 14
Check "install.ps1 completed" ($log -match 'claude-hr is installed')
$env:PATH = "$sb\toolbin;$env:PATH"
Check "claude-hr / claude-hr-ctl / claude-hr-hook installed" ((Test-Path "$sb\toolbin\claude-hr.exe") -and (Test-Path "$sb\toolbin\claude-hr-ctl.exe") -and (Test-Path "$sb\toolbin\claude-hr-hook.exe"))
Check "headroom installed" ((headroom --version 2>&1) -match '0\.\d+\.\d+')

"== 2. proxy =="
# the proxy is started through Headroom.start() under a watch for new console windows (0.2.1 opened an empty terminal window)
$probe = & "$sb\tooldir\claude-hr\Scripts\python.exe" "$repo\tests\windows\proxy_window_probe.py" --keep --restart --watch 300 2>&1 | Out-String
Check "starting the proxy opens no console window, and it comes up healthy" ($LASTEXITCODE -eq 0) $probe
$h = claude-hr-ctl headroom start --wait 60 2>&1 | Out-String
Check "proxy healthy" ($h -match 'healthy') $h
Check "compression smoke test" ((claude-hr-ctl headroom bench --wait 120 2>&1 | Out-String) -match 'PASS')

"== 3. managed config landed (sandbox profile) =="
$settings = Get-Content "$sb\h\.claude\settings.json" -Raw | ConvertFrom-Json
Check "baseline env present, no global base URL" (($settings.env.DISABLE_AUTOUPDATER -eq '1') -and (-not $settings.env.ANTHROPIC_BASE_URL))
Check "statusLine uses claude-hr-hook" ($settings.statusLine.command -match 'claude-hr-hook')
Check "CLAUDE.md import block" ((Get-Content "$sb\h\.claude\CLAUDE.md" -Raw) -match 'CLAUDE-HR:BEGIN')
$gj = Get-Content "$sb\h\.claude\.claude.json" -Raw | ConvertFrom-Json
Check "headroom + serena MCP registered" (($gj.mcpServers.PSObject.Properties.Name -contains 'headroom') -and ($gj.mcpServers.PSObject.Properties.Name -contains 'serena'))
$pl = claude plugin list --json 2>&1 | Out-String | ConvertFrom-Json
$names = @($pl | Where-Object { $_.scope -eq 'user' } | ForEach-Object { $_.id })
Check "standard profile plugins installed (lean: >=6, incl. hr-gates)" (($names.Count -ge 6) -and ($names -match '^hr-gates@')) ($names -join ', ')

"== 4. launcher + hooks =="
Check "passthrough --version" ((claude-hr --version 2>&1) -match 'Claude Code')
Remove-Item Env:CLAUDE_CONFIG_DIR, Env:CLAUDE_HR_USER_HOME   # use the REAL login for the model call; claude-hr state stays in the sandbox
$plan = claude-hr --hr-dry-run agents --dangerously-skip-permissions 2>&1 | Out-String
Check "agents routes via headroom" ($plan -match 'kind=agents' -and $plan -match 'route=headroom')
Set-Location "$sb\proj"
$out = claude-hr -p "Reply with exactly the word OK and nothing else." --model haiku --no-session-persistence --disable-slash-commands --output-format json 2>$null | Out-String
$ok = $false; try { $ok = (($out | ConvertFrom-Json).result.Trim() -eq 'OK') } catch {}
Check "real routed claude -p returns OK with clean JSON stdout" $ok $out
$stats = Invoke-RestMethod 'http://127.0.0.1:18788/stats?cached=1'
Check "request was counted by the proxy" ($stats.summary.api_requests -ge 1)
'{"tool_input":{"command":"cat .env"}}' | claude-hr-hook bash-guard 2>&1 | Out-Null
Check "bash guard blocks cat .env (exit 2)" ($LASTEXITCODE -eq 2)
'{"tool_input":{"command":"cp .env.example .env"}}' | claude-hr-hook bash-guard 2>&1 | Out-Null
Check "bash guard allows cp .env.example .env" ($LASTEXITCODE -eq 0)
# the git guard reads commands as words: `git commit -n` skips hooks (ask); a message that only mentions the flag does not
$askOut = '{"tool_input":{"command":"git commit -n -m x"}}' | claude-hr-hook bash-guard 2>&1 | Out-String
Check "bash guard asks before git commit -n" ($askOut -match '"permissionDecision": "ask"') $askOut
$quietOut = '{"tool_input":{"command":"git commit -m ''fix: --no-verify edge case''"}}' | claude-hr-hook bash-guard 2>&1 | Out-String
Check "bash guard is quiet when a message only mentions --no-verify" ($quietOut.Trim() -eq '') $quietOut

"== 4b. end-of-turn gate in a REAL session (format + trusted check + audit trail) =="
$py = (Get-ChildItem "$sb\tooldir\claude-hr\Scripts\python.exe").FullName
$pyToml = $py -replace '\\', '/'
uv tool install ruff 2>&1 | Out-Null
Check "ruff available for the repo's formatter" ((ruff --version 2>&1) -match 'ruff \d')
$gp = "$sb\gateproj"
New-Item -ItemType Directory -Force -Path "$gp\.claude" | Out-Null
git -C $gp init -q 2>&1 | Out-Null
git -C $gp config user.email t@example.com; git -C $gp config user.name t; git -C $gp config commit.gpgsign false
[IO.File]::WriteAllText("$gp\pyproject.toml", "[tool.ruff]`nline-length = 88`n")
$recipe = @"
[[fast]]
name = "needs-done-file"
argv = ["$pyToml", "-c", "import os,sys; ok=os.path.exists('done.txt'); print('' if ok else 'done.txt is missing: create it containing the word done'); sys.exit(0 if ok else 1)"]
timeout_s = 60
"@
[IO.File]::WriteAllText("$gp\.claude\hr-verify.toml", $recipe)
git -C $gp add -A 2>&1 | Out-Null
git -C $gp commit -q -m init 2>&1 | Out-Null
Push-Location $gp
$env:CLAUDECODE = '1'
$o = claude-hr-ctl verify trust 2>&1 | Out-String; $rc = $LASTEXITCODE
Remove-Item Env:CLAUDECODE
Check "verify trust refuses a Claude Code shell (no --yes exists)" (($rc -eq 1) -and ($o -match 'refusing to trust')) $o
$o = claude-hr-ctl verify status 2>&1 | Out-String
Check "recipe is NOT trusted before a person approves it" ($o -match 'NOT trusted') $o
'{"cwd":"' + ($gp -replace '\\', '\\') + '","hook_event_name":"Stop"}' | claude-hr-hook stop 2>&1 | Out-Null
Check "the gate never ran the untrusted recipe" (-not (Test-Path "$gp\done.txt"))
& $py -c "from pathlib import Path; from claude_hr import verify, paths; verify.trust(Path(r'$gp'), paths.state_dir())"
$o = claude-hr-ctl verify status 2>&1 | Out-String
Check "recipe trusted (what a person does with `verify trust` in a terminal)" ($o -match 'TRUSTED' -and $o -notmatch 'NOT trusted') $o
Check "fmt lists the repo's own formatter" ((claude-hr-ctl fmt 2>&1 | Out-String) -match 'ruff')
$out = claude-hr -p "Create the file hello.py containing exactly the single line: x   =   1   (three spaces around the equals sign). Nothing else. If a hook tells you something is missing, fix it." --model haiku --plugin-dir "$repo\plugins\hr-gates" --permission-mode acceptEdits --allowedTools "Write,Edit" --no-session-persistence --max-turns 10 --output-format json 2>$null | Out-String
Pop-Location
$hello = ''; if (Test-Path "$gp\hello.py") { $hello = (Get-Content "$gp\hello.py" -Raw) -replace "`r`n", "`n" }
Check "the hook formatted the file Claude wrote with the repo's ruff" ($hello -eq "x = 1`n") ("got: " + ($hello -replace "`n", '\n'))
Check "Claude was told the trusted check failed and fixed it" (Test-Path "$gp\done.txt") $out
$trail = claude-hr-ctl trail --tail 10 2>&1 | Out-String
Check "audit trail shows a block, then a pass" (($trail -match 'verify=block') -and ($trail -match 'verify=pass')) $trail
Check "the trail holds no paths or code" (($trail -notmatch 'hello\.py') -and ($trail -notmatch [regex]::Escape($gp)))

"== 5. management commands =="
$env:CLAUDE_CONFIG_DIR = "$sb\h\.claude"; $env:CLAUDE_HR_USER_HOME = "$sb\h"
Set-Location $repo
$doc = claude-hr-ctl doctor 2>&1 | Out-String
Check "doctor has no FAIL" ($doc -notmatch '\[FAIL\]') (($doc -split "`n" | Where-Object { $_ -match '\[FAIL\]' }) -join '; ')
Check "status" ((claude-hr-ctl status 2>&1 | Out-String) -match 'Headroom')
Check "plugins audit" ((claude-hr-ctl plugins audit 2>&1 | Out-String) -match 'always-on')
Check "claudemd lint" ((claude-hr-ctl claudemd lint 2>&1 | Out-String) -match 'always-loaded')
Check "update --check" ((claude-hr-ctl update --check --only claude headroom 2>&1 | Out-String) -match 'claude')
git -C "$sb\proj" init -q 2>&1 | Out-Null
$wf = claude-hr-ctl workflows install --dir "$sb\proj" 2>&1 | Out-String
Check "workflows install (both workflows)" ((Test-Path "$sb\proj\.claude\workflows\hr-review-changes.js") -and (Test-Path "$sb\proj\.claude\workflows\hr-plan-execute.js")) $wf
$fp = claude-hr-ctl footprint 2>&1 | Out-String
Check "footprint measures the always-on tokens (no cap)" (($fp -match 'Estimated always-on') -and ($fp -notmatch 'OVER')) $fp
Push-Location "$sb\proj"
[IO.File]::WriteAllText("$sb\proj\pyproject.toml", "[project]`nname = `"x`"`nversion = `"0`"`n")
$ir = claude-hr-ctl init-repo --only docs,editor 2>&1 | Out-String
Check "init-repo is a dry run" (($ir -match 'dry run') -and -not (Test-Path "$sb\proj\docs\plans")) $ir
claude-hr-ctl audit 2>&1 | Out-Null
Check "audit runs (exit 0)" ($LASTEXITCODE -eq 0)
Pop-Location
Check "monitor tick" ((claude-hr-ctl monitor tick 2>&1 | Out-String) -match 'alerts')

"== 6. shim round-trip with hostile arguments =="
claude-hr-ctl shim install 2>&1 | Out-Null
$shim = "$sb\h\.claude-hr\bin\claude.exe"
Check "shim is a real exe" (Test-Path $shim)
$py = (Get-ChildItem "$sb\tooldir\claude-hr\Scripts\python.exe").FullName
$r = & python -c "import subprocess,json,os,sys; env=dict(os.environ,CLAUDE_HR_CLAUDE_BIN=r'$py',CLAUDE_HR_DIRECT='1',CLAUDE_HR_HEADROOM__ENABLED='false'); env.pop('CLAUDE_HR_SESSION',None); t=['x&y','a|b','a>b.txt','50%PATH%','line1\nline2','^caret']; p=subprocess.run([r'$shim','-c','import sys,json;print(json.dumps(sys.argv[1:]))',*t],env=env,capture_output=True,text=True); print('EXACT' if json.loads(p.stdout.strip().splitlines()[-1])==t else 'MISMATCH')" 2>&1
Check "hostile args pass through exactly" ($r -match 'EXACT') $r

"== 7. uninstall restores the profile =="
$u = claude-hr-ctl uninstall 2>&1 | Out-String
$after = Get-Content "$sb\h\.claude\settings.json" -Raw
Check "managed settings removed" ($after -notmatch 'DISABLE_AUTOUPDATER|CLAUDE_HR_MANAGED')
Check "CLAUDE.md block removed" ((-not (Test-Path "$sb\h\.claude\CLAUDE.md")) -or ((Get-Content "$sb\h\.claude\CLAUDE.md" -Raw) -notmatch 'CLAUDE-HR:BEGIN'))

"== real profile untouched? =="
Check "the real ~/.claude/settings.json and ~/.claude-hr (config, trust, offer records) are unchanged by this run" ((RealProfileSnapshot) -eq $realBefore)
$tasksAfter = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
Check "scheduler tasks unchanged (none created, none of yours removed)" ($tasksAfter -eq $tasksBefore) "before: $tasksBefore after: $tasksAfter"
"WIN_RESULT: $pass passed, $fail failed"

# teardown: proxy, sandbox (nothing here starts a Claude daemon, so none is stopped: it could be the user's own)
Get-NetTCPConnection -LocalPort 18788 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { taskkill /PID $_.OwningProcess /T /F | Out-Null }
Set-Location 'C:\'
Remove-Item -Recurse -Force $sb -ErrorAction SilentlyContinue
"sandbox removed: $(-not (Test-Path $sb))"
"WIN_DONE"
