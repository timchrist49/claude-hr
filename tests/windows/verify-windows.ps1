# Windows end-to-end verification of claude-hr from a clean, isolated sandbox (Windows PowerShell 5.1 or 7):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\verify-windows.ps1
# Installs into %TEMP%\esb3 (own uv tool dir, Claude config dir and claude-hr home), runs ~30 checks incl. the real Headroom
# install and ONE tiny `claude -p` call with your real login, then uninstalls and deletes the sandbox. It never writes
# to your real ~/.claude or ~/.claude-hr, and it verifies that afterwards. Takes ~3-5 minutes (about 0.5 GB download, 2 GB on disk).
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$sb = Join-Path $env:TEMP 'esb3'
if (Test-Path $sb) { Remove-Item -Recurse -Force $sb }
New-Item -ItemType Directory -Force -Path "$sb\h\.claude", "$sb\tooldir", "$sb\toolbin", "$sb\proj" | Out-Null
$realConfig = "$env:USERPROFILE\.claude"
$tasksBefore = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
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
$h = claude-hr-ctl headroom start --wait 300 2>&1 | Out-String
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
Check "standard profile plugins installed (>=8)" (@($pl | Where-Object { $_.scope -eq 'user' }).Count -ge 8) "$(@($pl).Count) found"

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
Check "workflows install" (Test-Path "$sb\proj\.claude\workflows\hr-review-changes.js") $wf
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
Check "no ~/.claude-hr" (-not (Test-Path "$env:USERPROFILE\.claude-hr"))
Check "no claude-hr keys in real settings.json" ((Get-Content "$realConfig\settings.json" -Raw) -notmatch 'CLAUDE_HR_MANAGED')
$tasksAfter = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
Check "scheduler tasks unchanged (none created, none of yours removed)" ($tasksAfter -eq $tasksBefore) "before: $tasksBefore after: $tasksAfter"
"WIN_RESULT: $pass passed, $fail failed"

# teardown: proxy, sandbox (nothing here starts a Claude daemon, so none is stopped: it could be the user's own)
Get-NetTCPConnection -LocalPort 18788 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { taskkill /PID $_.OwningProcess /T /F | Out-Null }
Set-Location 'C:\'
Remove-Item -Recurse -Force $sb -ErrorAction SilentlyContinue
"sandbox removed: $(-not (Test-Path $sb))"
"WIN_DONE"
