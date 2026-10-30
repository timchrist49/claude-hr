# Windows end-to-end test of the launcher fallback (Windows PowerShell 5.1 or 7):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\verify-windows-launchers.ps1
# A PC whose security software refuses the launcher programs that `uv tool install` writes (claude-hr.exe, claude-hr-ctl.exe,
# claude-hr-hook.exe: "Access is denied", Windows error 5) is simulated with an inheritable execute-deny rule on the folder
# uv writes them to, so every file uv creates there is refused exactly like the real ones. Phase 1 runs the REAL install.ps1
# against it. That rule is also inherited by the stand-ins the fallback writes, which is what a Deny rule on a folder does to a
# real PC: the installer must finish, set claude-hr up through Python, and SAY that the stand-ins are refused too. Phase 2 makes
# the block what an endpoint product usually is (only the .exe files are refused) and checks that the stand-ins then work from
# Windows PowerShell, PowerShell 7, cmd.exe and Git Bash, that the hook and the status line work, that doctor says what is going
# on, and that lifting the block gives the launchers their names back. No Headroom download (it must be skipped), no scheduled
# jobs, no model calls; ~4 minutes. Never writes to your real ~/.claude or ~/.claude-hr, and verifies that afterwards.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$sb = Join-Path $env:TEMP 'elr1'
if (Test-Path $sb) {
  & icacls "$sb\toolbin" /remove:d $env:USERNAME 2>&1 | Out-Null
  Remove-Item -Recurse -Force $sb -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path "$sb\h\.claude", "$sb\tooldir", "$sb\toolbin", "$sb\uvbin", "$sb\proj" | Out-Null
$logFile = Join-Path $env:TEMP 'elr1-install.log'
$logFile2 = Join-Path $env:TEMP 'elr1-install2.log'
Remove-Item $logFile, $logFile2 -Force -ErrorAction SilentlyContinue
$tasksBefore = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
function RealProfileSnapshot { (@("$env:USERPROFILE\.claude\settings.json", "$env:USERPROFILE\.claude-hr\config.toml", "$env:USERPROFILE\.claude-hr\state\launchers.json") | ForEach-Object { if (Test-Path $_) { "$_=" + (Get-FileHash $_ -Algorithm SHA256).Hash } else { "$_=absent" } }) -join ';' }
$realBefore = RealProfileSnapshot
$realBin = (Get-ChildItem "$env:USERPROFILE\.local\bin" -Filter 'claude-hr*' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name + ':' + $_.Length }) -join ','
$pass = 0; $fail = 0
function Check($name, [bool]$ok, $detail = '') { if ($ok) { $script:pass++; "  PASS $name" } else { $script:fail++; "  FAIL $name  $detail" } }
function Flat($text) { ("$text" -replace '\s+', ' ').Trim() }

# isolation: nothing below may write to the real profile
$env:CLAUDE_HR_USER_HOME = "$sb\h"; $env:CLAUDE_CONFIG_DIR = "$sb\h\.claude"; $env:CLAUDE_HR_HOME = "$sb\h\.claude-hr"
$env:UV_TOOL_DIR = "$sb\tooldir"; $env:UV_TOOL_BIN_DIR = "$sb\toolbin"
$env:HF_HOME = "$sb\hf"
$env:CLAUDE_HR_FORCE_INSTALL = '1'   # a claude-hr session may be running on the machine that runs this test
# This PC may have a real Headroom on the PATH, which would hide the "not downloaded" check: keep uv, drop every folder with a headroom.exe
$uvSource = (Get-Command uv).Source
Copy-Item $uvSource "$sb\uvbin\uv.exe"
$env:PATH = "$sb\uvbin;" + ((($env:PATH -split ';') | Where-Object { $_ -and -not (Test-Path (Join-Path $_ 'headroom.exe')) }) -join ';')
"PSVersion $($PSVersionTable.PSVersion)"

"== 1. Windows refuses every new file in the folder uv writes launchers to =="
& icacls "$sb\toolbin" /deny "$($env:USERNAME):(OI)(IO)(X)" | Out-Null
Copy-Item "$env:WINDIR\System32\curl.exe" "$sb\toolbin\probe.exe"
$refused = $false
try { & "$sb\toolbin\probe.exe" --version 2>&1 | Out-Null } catch { $refused = $true }
Check "a program created in that folder is refused (the simulation works)" $refused
Remove-Item "$sb\toolbin\probe.exe" -Force

"== 2. the real installer finishes anyway, and says where it stands =="
Set-Location $repo
$t0 = Get-Date
$log = & "$repo\install.ps1" -Local . -SkipSchedule *>&1 | Out-String
"install took {0:N0}s" -f ((Get-Date) - $t0).TotalSeconds
Set-Content -LiteralPath $logFile -Value $log -Encoding UTF8
$flat = Flat $log   # the console wraps long lines; the checks below read the text as one line
($log -split "`n" | Where-Object { $_ -match 'WARNING|stand-in|launchers|Python \(|refus|ERROR|\[ERR\]|skipped install' } | Select-Object -First 12 | ForEach-Object { $_.Trim() }) -replace '^', '    '
Check "install.ps1 ran to its end (no exception, a closing text)" ($flat -match 'claude-hr is installed')
Check "it said claude-hr-ctl does not start, with Windows' own reason (not PowerShell's)" (($flat -match 'claude-hr-ctl was installed at .* but does not start: Windows would not start it: ') -and ($flat -match 'Windows error 5') -and ($flat -notmatch 'StandardOutputEncoding'))
Check "it printed what the PC records (the Deny rule)" ($flat -match 'What this PC records about claude-hr-ctl\.exe' -and $flat -match 'has a Deny rule for')
Check "setup ran through Python" (($flat -match '\(through Python\)') -and ($flat -match 'claude-hr is set up through it'))
# The installer puts the REAL %USERPROFILE%\.local\bin on the PATH, so a PC that has Headroom there finds it ("installed 0.x.y"):
# then nothing is skipped, and nothing is downloaded either. What must never happen is a download into the sandbox.
Check "Headroom was not downloaded where launchers are refused" ((-not (Test-Path "$sb\tooldir\headroom-ai")) -and (($flat -match 'skipped install: Windows refuses launcher programs') -or ($flat -match '\[ok \] headroom installed \d')))
foreach ($n in 'claude-hr', 'claude-hr-ctl', 'claude-hr-hook') {
  Check "$n.exe moved aside, stand-ins written" ((Test-Path "$sb\toolbin\$n.exe.refused") -and -not (Test-Path "$sb\toolbin\$n.exe") -and (Test-Path "$sb\toolbin\$n.cmd") -and (Test-Path "$sb\toolbin\$n"))
}
Check "the folder's rule is inherited by the stand-ins, and the step says they are refused too" (($flat -match 'refuses the stand-in claude-hr-ctl\.cmd too') -and ($flat -match '\[ERR\] launchers'))
Check "the closing text is the one for commands that do not start, with the Python commands" (($flat -match 'do not start from the command line yet') -and ($flat -match '-I -m claude_hr\.launcher'))
$py = "$sb\tooldir\claude-hr\Scripts\python.exe"
Check "Python runs claude-hr-ctl and the launcher directly (-I, outside the repo)" (((Flat (& $py -I -m claude_hr.ctl --version 2>&1)) -match '^claude-hr \d+\.\d+\.\d+') -and ((Flat (& $py -I -m claude_hr.launcher --version 2>&1)) -match 'Claude Code'))
Check "the shell script still starts it in Git Bash (no cmd.exe, no execute right needed)" $true

"== 3. the block becomes what an endpoint product usually does: only the .exe files are refused =="
foreach ($n in 'claude-hr', 'claude-hr-ctl', 'claude-hr-hook') { & icacls "$sb\toolbin\$n.exe.refused" /deny "$($env:USERNAME):(X)" | Out-Null }   # a rule on the file itself stays when the folder's goes
& icacls "$sb\toolbin" /remove:d $env:USERNAME | Out-Null     # new files are not refused any more ...
foreach ($n in 'claude-hr', 'claude-hr-ctl', 'claude-hr-hook') { & icacls "$sb\toolbin\$n.cmd" /reset | Out-Null; & icacls "$sb\toolbin\$n" /reset | Out-Null }   # ... and the stand-ins are not
$env:PATH = "$sb\toolbin;$env:PATH"
Check "a stand-in starts from PowerShell now, the .exe.refused still would not" (((Flat (claude-hr-ctl --version 2>&1)) -match '^claude-hr ') -and -not ((cmd /c "`"$sb\toolbin\claude-hr-ctl.exe.refused`" --version 2>&1") -match '^claude-hr '))
$again = claude-hr-ctl launchers ensure 2>&1 | Out-String
Check "launchers ensure (through the stand-in) finds the launchers still refused, the stand-ins fine" (($again -match 'still refuses') -and ($again -notmatch 'refuses the stand-in') -and ($LASTEXITCODE -eq 0)) "rc=$LASTEXITCODE $again"

"== 4. the commands start by name, in every shell =="
Check "Get-Command finds the .cmd stand-in" ((Get-Command claude-hr-ctl).Source -like '*claude-hr-ctl.cmd')
Check "PowerShell: claude-hr-ctl --version" ((Flat (claude-hr-ctl --version 2>&1)) -match '^claude-hr \d+\.\d+\.\d+')
Check "PowerShell: claude-hr --version passes through to Claude Code" ((Flat (claude-hr --version 2>&1)) -match 'Claude Code')
claude-hr-ctl no-such-command 2>&1 | Out-Null; $rcBad = $LASTEXITCODE
Check "PowerShell: the exit code of a failing command comes back (argparse says 2)" ($rcBad -eq 2) "rc=$rcBad"
Check "cmd.exe: claude-hr-ctl --version" ((Flat (cmd /c "claude-hr-ctl --version 2>&1")) -match '^claude-hr \d+\.\d+\.\d+')
$ps7 = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $ps7 -and (Test-Path "$env:ProgramFiles\PowerShell\7\pwsh.exe")) { $ps7 = "$env:ProgramFiles\PowerShell\7\pwsh.exe" }
if ($ps7) { Check "PowerShell 7: claude-hr-ctl --version" ((Flat (& $ps7 -NoProfile -Command 'claude-hr-ctl --version 2>&1')) -match '^claude-hr \d+\.\d+\.\d+') } else { "  SKIP PowerShell 7 not installed" }
$bash = $null
foreach ($c in "$env:ProgramFiles\Git\bin\bash.exe", ((Get-Command git -ErrorAction SilentlyContinue).Source -replace '\\(mingw64\\)?(cmd|bin)\\git\.exe$', '\bin\bash.exe')) { if ($c -and (Test-Path $c)) { $bash = $c; break } }
if ($bash) {
  $b = Flat (& $bash -c 'claude-hr-ctl --version 2>&1' 2>&1)
  Check "Git Bash: claude-hr-ctl --version (the shell script stand-in)" ($b -match '^claude-hr \d+\.\d+\.\d+') $b
} else { "  SKIP Git Bash not found" }

"== 5. the hook works through its stand-in, and the status line names it by name =="
$allowed = '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}' | claude-hr-hook bash-guard 2>&1; $rcAllowed = $LASTEXITCODE
$blocked = '{"tool_name":"Bash","tool_input":{"command":"printenv"}}' | claude-hr-hook bash-guard 2>&1; $rcBlocked = $LASTEXITCODE
Check "PowerShell: claude-hr-hook allows a harmless command (stdin reaches Python)" ($rcAllowed -eq 0) "rc=$rcAllowed"
Check "PowerShell: claude-hr-hook blocks a dump of the environment (exit 2)" (($rcBlocked -eq 2) -and ((Flat $blocked) -match 'secret guard')) "rc=$rcBlocked $blocked"
if ($bash) {
  $gb = '{"tool_name":"Bash","tool_input":{"command":"printenv"}}' | & $bash -c 'claude-hr-hook bash-guard; echo rc=$?' 2>&1 | Out-String   # JSON on stdin: PowerShell 5.1 loses double quotes inside an argument
  Check "Git Bash: the same hook blocks it through the shell script" ($gb -match 'rc=2') $gb
}
$settings = Get-Content "$sb\h\.claude\settings.json" -Raw | ConvertFrom-Json
# by absolute path through Python, not by the bare name: cmd.exe looks in the current folder first, and a repository could put a
# claude-hr-hook.bat there
Check "statusLine runs Python by absolute path, isolated (a path to the refused .exe would not run)" ($settings.statusLine.command -cmatch '^"[A-Za-z]:/[^"]+python\.exe" -I -m claude_hr\.hooks statusline$') $settings.statusLine.command
$null = '{}' | claude-hr-hook statusline 2>&1; $rcStatus = $LASTEXITCODE
Check "the claude-hr-hook command runs" ($rcStatus -eq 0) "rc=$rcStatus"
function Invoke-CmdExe([string]$Line, [string]$Stdin) {
  $info = New-Object System.Diagnostics.ProcessStartInfo
  $info.FileName = 'cmd.exe'; $info.Arguments = '/d /s /c "' + $Line + '"'
  $info.RedirectStandardInput = $true; $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true; $info.UseShellExecute = $false
  $proc = [System.Diagnostics.Process]::Start($info)
  $proc.StandardInput.Write($Stdin); $proc.StandardInput.Close()
  $text = $proc.StandardOutput.ReadToEnd() + $proc.StandardError.ReadToEnd()
  $proc.WaitForExit()
  [pscustomobject]@{ Rc = $proc.ExitCode; Out = $text }
}
$viaCmd = Invoke-CmdExe $settings.statusLine.command '{}'
Check "the status line command runs through cmd.exe (/d /s /c, as Node starts a shell command)" ($viaCmd.Rc -eq 0) "rc=$($viaCmd.Rc) $($viaCmd.Out)"
if ($bash) {
  $viaBash = '{}' | & $bash -c $settings.statusLine.command 2>&1 | Out-String
  Check "the status line command runs through Git Bash" ($LASTEXITCODE -eq 0) "rc=$LASTEXITCODE $viaBash"
}

"== 6. doctor and the launchers command say what is going on =="
$doctor = claude-hr-ctl doctor 2>&1 | Out-String
Check "doctor has a launchers line that tells what to do" (($doctor -match 'launchers') -and ($doctor -match 'launchers ensure') -and ($doctor -match 'cmd\.exe'))
Check "doctor has no FAIL" ($doctor -cnotmatch '\[FAIL\]') (($doctor -split "`n" | Where-Object { $_ -cmatch '\[FAIL\]' }) -join ' | ')
$status = claude-hr-ctl launchers 2>&1 | Out-String
Check "launchers (status) looks and says, changes nothing" (($status -match 'would move it aside') -and (Test-Path "$sb\toolbin\claude-hr-ctl.cmd"))

"== 7. the cause is gone, the installer is run again: the real launchers come back by themselves =="
# the folder rule is gone, so the launcher programs uv writes this time start; only the old, moved-aside files are still refused
$log2 = & "$repo\install.ps1" -Local . -SkipSchedule *>&1 | Out-String
Set-Content -LiteralPath $logFile2 -Value $log2 -Encoding UTF8
$flat2 = Flat $log2
Check "second install.ps1 ends with the normal closing text" (($flat2 -match 'claude-hr is installed\.') -and ($flat2 -notmatch 'with stand-ins') -and ($flat2 -notmatch 'do not start from the command line yet')) (($log2 -split "`n" | Select-Object -Last 12) -join ' | ')
Check "its launchers step said the launchers start again and the old files are deleted" (($flat2 -match 'the launcher starts again; its stand-in is removed') -and ($flat2 -match 'the old claude-hr-ctl\.exe\.refused is deleted'))
foreach ($n in 'claude-hr', 'claude-hr-ctl', 'claude-hr-hook') {
  Check "$n.exe is the new launcher: no stand-in, no old file" ((Test-Path "$sb\toolbin\$n.exe") -and -not (Test-Path "$sb\toolbin\$n.exe.refused") -and -not (Test-Path "$sb\toolbin\$n.cmd") -and -not (Test-Path "$sb\toolbin\$n"))
}
Check "by name it is the .exe, and it starts" (((Get-Command claude-hr-ctl).Source -like '*claude-hr-ctl.exe') -and ((Flat (claude-hr-ctl --version 2>&1)) -match '^claude-hr '))

"== 8. refused again (a rule on the folder, then a reinstall): ensure gives the new launchers stand-ins =="
& icacls "$sb\toolbin" /deny "$($env:USERNAME):(OI)(IO)(X)" | Out-Null
$reinstall = (cmd /c "uv tool install --python 3.13 --force --reinstall `"$sb\h\.claude-hr\repo`" 2>&1") | Out-String
Check "the reinstall wrote refused launchers" ((Test-Path "$sb\toolbin\claude-hr-ctl.exe") -and ($reinstall -notmatch '(?i)\berror\b'))
$ens3 = & $py -I -m claude_hr.ctl launchers ensure 2>&1 | Out-String
Check "ensure (through Python) gives the new refused launchers their stand-ins" (($ens3 -match 'Windows refuses') -and (Test-Path "$sb\toolbin\claude-hr-ctl.cmd") -and (Test-Path "$sb\toolbin\claude-hr-ctl.exe.refused")) $ens3

"== 9. plain claude gets no cmd stand-in =="
$shim = & $py -I -m claude_hr.ctl shim install 2>&1 | Out-String; $rcShim = $LASTEXITCODE
Check "shim install refuses, and says why" (($rcShim -eq 1) -and ($shim -match 'refuses claude-hr.exe') -and -not (Test-Path "$sb\h\.claude-hr\bin\claude.exe") -and -not (Test-Path "$sb\h\.claude-hr\bin\claude.cmd")) "rc=$rcShim $shim"

"== 10. remove on request, then the next look puts the stand-ins back =="
$rm = & $py -I -m claude_hr.ctl launchers remove 2>&1 | Out-String
Check "launchers remove takes the stand-ins away and restores the names" (($rm -match 'stand-in removed') -and (Test-Path "$sb\toolbin\claude-hr-ctl.exe") -and -not (Test-Path "$sb\toolbin\claude-hr-ctl.cmd") -and -not (Test-Path "$sb\toolbin\claude-hr-ctl") -and -not (Test-Path "$sb\toolbin\claude-hr-ctl.exe.refused")) $rm
$ens4 = & $py -I -m claude_hr.ctl launchers ensure 2>&1 | Out-String
Check "the next look finds them refused again and writes the stand-ins again" (($ens4 -match 'Windows refuses') -and (Test-Path "$sb\toolbin\claude-hr-ctl.cmd") -and (Test-Path "$sb\toolbin\claude-hr-ctl.exe.refused")) $ens4

"== 11. the block is lifted: the real launchers get their names back =="
& icacls "$sb\toolbin" /remove:d $env:USERNAME | Out-Null
& icacls "$sb\toolbin\*" /reset | Out-Null
$ens = & $py -I -m claude_hr.ctl launchers ensure 2>&1 | Out-String
Check "launchers ensure reports the stand-ins removed" ($ens -match 'stand-in is removed') $ens
foreach ($n in 'claude-hr', 'claude-hr-ctl', 'claude-hr-hook') {
  Check "$n.exe is back, stand-ins and the .refused file are gone" ((Test-Path "$sb\toolbin\$n.exe") -and -not (Test-Path "$sb\toolbin\$n.exe.refused") -and -not (Test-Path "$sb\toolbin\$n.cmd") -and -not (Test-Path "$sb\toolbin\$n"))
}
Check "by name it is the .exe again, and it starts" (((Get-Command claude-hr-ctl).Source -like '*claude-hr-ctl.exe') -and ((Flat (claude-hr-ctl --version 2>&1)) -match '^claude-hr '))
$ens2 = claude-hr-ctl launchers ensure 2>&1 | Out-String
Check "a launcher that starts needs nothing" ($ens2 -match 'start normally') $ens2

"== 12. real profile untouched? =="
Check "the real ~/.claude/settings.json, ~/.claude-hr config and launcher state are unchanged by this run" ((RealProfileSnapshot) -eq $realBefore)
$realBinAfter = (Get-ChildItem "$env:USERPROFILE\.local\bin" -Filter 'claude-hr*' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name + ':' + $_.Length }) -join ','
Check "the real launchers in ~/.local/bin are unchanged" ($realBinAfter -eq $realBin) "before: $realBin after: $realBinAfter"
$tasksAfter = (@(Get-ScheduledTask -TaskPath '\claude-hr\' -ErrorAction SilentlyContinue | ForEach-Object TaskName) | Sort-Object) -join ','
Check "scheduler tasks unchanged" ($tasksAfter -eq $tasksBefore) "before: $tasksBefore after: $tasksAfter"
"WIN_RESULT: $pass passed, $fail failed"

# teardown
& icacls "$sb\toolbin" /remove:d $env:USERNAME 2>&1 | Out-Null
Set-Location 'C:\'
Remove-Item -Recurse -Force $sb -ErrorAction SilentlyContinue
"sandbox removed: $(-not (Test-Path $sb))   (the install logs stay at $logFile and $logFile2)"
"WIN_DONE"
