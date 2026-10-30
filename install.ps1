<#
.SYNOPSIS
  claude-hr bootstrap installer for Windows (PowerShell 5.1+ / 7).
.DESCRIPTION
  Installs prerequisites (uv, Claude Code), fetches the claude-hr source, installs the claude-hr and
  claude-hr-ctl commands and runs `claude-hr-ctl setup` (idempotent). Needs git (Claude Code needs it too).
.EXAMPLE
  irm https://raw.githubusercontent.com/timchrist49/claude-hr/main/install.ps1 | iex
  .\install.ps1 -Local .
.NOTES
  Private fork: set $env:CLAUDE_HR_GIT_TOKEN (fine-grained PAT, Contents:read). Used for the clone only,
  via a one-shot header, never written to disk. For unattended updates use an SSH deploy key URL.
#>
[CmdletBinding()]
param(
  [string]$Repo = $(if ($env:CLAUDE_HR_REPO_URL) { $env:CLAUDE_HR_REPO_URL } else { 'https://github.com/timchrist49/claude-hr.git' }),
  [string]$Ref = $(if ($env:CLAUDE_HR_REF) { $env:CLAUDE_HR_REF } else { 'main' }),
  [string]$Local = '',
  [ValidateSet('', 'minimal', 'standard', 'process', 'audit', 'full', 'lean-output')][Alias('Profile')][string]$PluginProfile = '',
  [switch]$NoHeadroomInstall,
  [switch]$SkipSchedule,
  [switch]$Shim,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
function Log($m) { Write-Host "[claude-hr-install] $m" -ForegroundColor Cyan }
function Die($m) { Write-Host "[claude-hr-install] ERROR: $m" -ForegroundColor Red; throw "claude-hr-install failed: $m" }
# Windows PowerShell 5.1 turns ANY native-command stderr line (uv's "Resolved 1 package", git progress) into a terminating
# error under $ErrorActionPreference='Stop'. Run native tools with 'Continue' and decide from $LASTEXITCODE instead.
function N([scriptblock]$Block) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { & $Block 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $prev }
}

# ---- downloads: BEGIN (tests/test_installer_ps1.py runs this block under the real shell, with the network replaced) ---------
# A host that cannot be reached must not end the install: every download below has a second route, and when all fail the
# message says what was tried and which hosts this PC can reach. ("Unable to connect to the remote server" alone does not
# even say which host.)
# Older .NET (before 4.7) offers a host only TLS 1.0/1.1 unless told otherwise. 0 = "the system decides" (.NET 4.7+): left alone.
try { $tls = [int][Net.ServicePointManager]::SecurityProtocol; if ($tls -ne 0 -and -not ($tls -band 3072)) { [Net.ServicePointManager]::SecurityProtocol = $tls -bor 3072 } } catch { }

function Test-HaveCommand([string]$Name) { [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

# The reason in an exception message, without the "At line:..." / "At C:\...ps1:3" trace that PowerShell glues on right after
# the text of a program that failed to start ("...Access is deniedAt line:201 char:24"). Case-sensitive, and only where the
# "At" is attached to the text before it (or starts a line), so a sentence that says "... at C:\data ..." is left alone.
function Get-ErrorReason([string]$Message) { ($Message -creplace '(?s)(?<=\S|\n)At (line:\d+|[A-Za-z]:\\).*$', '').Trim() }

# Why Windows will not start the program at $Path, asked of Windows itself ($null when it does start). PowerShell does not
# report a refusal the same way everywhere: Windows PowerShell 5.1 says "Access is denied", PowerShell 7 says
# "StandardOutputEncoding is only supported when standard output is redirected" for the very same refusal (it starts the
# program a second way when the first fails, and that second start is what throws).
function Get-StartFailure([string]$Path) {
  try {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Path; $info.Arguments = '--version'; $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true; $info.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($info)
    try { if (-not $proc.WaitForExit(10000)) { try { $proc.Kill() } catch { } } } finally { $proc.Dispose() }
    return $null
  } catch {
    $e = $_.Exception
    while ($e.InnerException) { $e = $e.InnerException }
    $code = if ($e -is [System.ComponentModel.Win32Exception]) { $e.NativeErrorCode } else { 0 }
    if ($code) { return ((New-Object System.ComponentModel.Win32Exception $code).Message.TrimEnd('.') + " (Windows error $code)") }
    return "$($e.Message)".Trim()
  }
}

# Run `<name> --version` the way a person would. "On the PATH" is not "works": Windows can refuse to start a program that is
# there (antivirus, AppLocker or other application control, a scan that has not finished), and PowerShell raises that as an
# exception - "Program 'claude.exe' failed to run: Access is denied" - not as an exit code. A program that cannot be started
# is tried again a few times (a virus scanner may still hold a file written a moment ago); one that starts and fails is not.
function Test-ToolRuns([string]$Name) {
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $cmd) { return @{ Ok = $false; Path = ''; Version = ''; Error = "'$Name' is not on the PATH" } }
  $path = if ($cmd.Source) { $cmd.Source } else { "$($cmd.Definition)" }
  $why = ''
  for ($try = 1; $try -le 4; $try++) {
    try {
      $out = @(N { & $Name --version })
      if ($LASTEXITCODE -eq 0) { return @{ Ok = $true; Path = $path; Version = "$($out | Select-Object -First 1)"; Error = '' } }
      return @{ Ok = $false; Path = $path; Version = ''; Error = "it started but exited with code $LASTEXITCODE" }
    } catch {
      $why = Get-ErrorReason $_.Exception.Message
      $native = Get-StartFailure $path  # PowerShell 7 hides the real reason behind a message about output encodings
      if ($native) { $why = "Windows would not start it: $native" }
    }
    if ($try -lt 4) { Start-Sleep -Seconds 5 }
  }
  @{ Ok = $false; Path = $path; Version = ''; Error = $why }
}

# A new folder that only this user can write to, even when TEMP points at a shared one: what is checked here is what is run.
function New-StagingFolder {
  $root = if ($env:LOCALAPPDATA -and (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Temp'))) { Join-Path $env:LOCALAPPDATA 'Temp' } else { [IO.Path]::GetTempPath() }
  $dir = Join-Path $root ('claude-hr-' + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $dir
}

# Download $Url to $OutFile, two attempts. The error names the URL as well as the reason.
function Get-Download([string]$Url, [string]$OutFile) {
  $ProgressPreference = 'SilentlyContinue'  # the progress bar makes Windows PowerShell 5.1 downloads many times slower
  for ($attempt = 1; $attempt -le 2; $attempt++) {
    try {
      Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop | Out-Null
      return
    } catch {
      if ($attempt -eq 2) { throw "$Url - $($_.Exception.Message)" }
      Start-Sleep -Seconds 2
    }
  }
}

# True when something answers on $Url, even with an error page. Same request path (proxy settings, TLS) as the downloads.
function Test-Reachable([string]$Url) {
  try { Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop | Out-Null; return $true }
  catch { $r = $_.Exception.PSObject.Properties['Response']; return [bool]($r -and $r.Value) }
}

function Show-Reachability {
  Write-Host '[claude-hr-install] the hosts this install talks to, as this PC sees them (HTTPS, through your proxy settings):' -ForegroundColor Yellow
  foreach ($u in 'https://astral.sh', 'https://releases.astral.sh', 'https://github.com', 'https://release-assets.githubusercontent.com', 'https://pypi.org', 'https://files.pythonhosted.org', 'https://claude.ai') {
    $ok = Test-Reachable $u
    Write-Host ('  {0,-48} {1}' -f $u, $(if ($ok) { 'reachable' } else { 'NOT reachable' })) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
  }
}

# A program that an installer or winget just added to the PATH in the registry is not in this process's copy of it yet.
function Update-SessionPath {
  $links = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links' } else { '' }
  $registry = @([Environment]::GetEnvironmentVariable('Path', 'User'), [Environment]::GetEnvironmentVariable('Path', 'Machine'))
  $seen = @{}; $parts = @()
  foreach ($p in (@($localBin, $links, $env:PATH) + $registry) -split ';') {
    if ($p -and -not $seen[$p.TrimEnd('\').ToLower()]) { $seen[$p.TrimEnd('\').ToLower()] = $true; $parts += $p }
  }
  $env:PATH = $parts -join ';'
}

# Download a PowerShell installer and run it in a PowerShell of its own, not with `irm <url> | iex`: the installers of uv and
# Claude Code end with `exit 1` when they fail, and an `exit` run by Invoke-Expression closes the person's whole PowerShell
# window, fallbacks and error message included. What it printed is shown; a failure keeps its exit code and last lines.
function Invoke-DownloadedScript([string]$Url) {
  $dir = New-StagingFolder
  try {
    $file = Join-Path $dir 'installer.ps1'
    Get-Download $Url $file
    $bytes = [IO.File]::ReadAllBytes($file)  # a BOM, or Windows PowerShell 5.1 would read non-ASCII text as ANSI
    if (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) {
      [IO.File]::WriteAllBytes($file, [byte[]](0xEF, 0xBB, 0xBF) + $bytes)
    }
    $shell = (Get-Process -Id $PID).Path
    $last = New-Object System.Collections.ArrayList
    N { & $shell -NoProfile -ExecutionPolicy Bypass -File $file } | ForEach-Object {
      Write-Host "  $_"
      if ("$_".Trim()) { [void]$last.Add("$_".Trim()); if ($last.Count -gt 3) { $last.RemoveAt(0) } }
    }
    if ($LASTEXITCODE -ne 0) { throw ("the installer ended with exit code $LASTEXITCODE" + $(if ($last.Count) { ' (' + ($last -join ' | ') + ')' })) }
  } finally { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue }
}

function Install-WithWinget([string]$Id) {
  if (-not (Test-HaveCommand winget)) { throw 'winget is not installed' }
  Log "installing $Id with winget"
  $out = @(N { winget install --id $Id -e --source winget --accept-source-agreements --accept-package-agreements })
  if ($LASTEXITCODE -ne 0) {
    $tail = ($out | Where-Object { "$_".Trim().Length -gt 3 } | Select-Object -Last 2) -join ' | '
    throw "winget exited with code $LASTEXITCODE$(if ($tail) { ': ' + $tail })"
  }
}

# uv straight from its GitHub release (github.com has to work anyway: the claude-hr source is cloned from there). The
# checksum comes from the same release, so it catches a damaged or truncated download, not a hijacked release.
function Install-UvFromGitHub {
  $cpu = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
  $name = 'uv-' + $(switch ($cpu) { 'ARM64' { 'aarch64' } 'x86' { 'i686' } default { 'x86_64' } }) + '-pc-windows-msvc.zip'
  $base = 'https://github.com/astral-sh/uv/releases/latest/download'
  $tmp = New-StagingFolder
  try {
    Log "installing uv from GitHub ($name)"
    Get-Download "$base/$name" "$tmp\$name"
    Get-Download "$base/$name.sha256" "$tmp\$name.sha256"
    $want = (Get-Content -LiteralPath "$tmp\$name.sha256" -Raw) -split '\s+' | Where-Object { $_ } | Select-Object -First 1
    # .NET, not Get-FileHash / Expand-Archive: those are script functions that Windows PowerShell loads on first use, and on
    # a CI runner that load failed ("The term 'Get-FileHash' is not recognized"), which took the whole route down
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead("$tmp\$name")
    try { $got = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') } finally { $stream.Dispose(); $sha.Dispose() }
    if ("$want" -notmatch '^[0-9a-fA-F]{64}$' -or $got -ne $want) { throw "the checksum of $name does not match (expected $want, got $got)" }
    New-Item -ItemType Directory -Force -Path $localBin | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead("$tmp\$name")
    try {  # only these three, by name: whatever else a zip holds, or a path that climbs out of the folder, is never written
      foreach ($entry in $zip.Entries) {
        if ($entry.Name -in 'uv.exe', 'uvx.exe', 'uvw.exe') { [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $localBin $entry.Name), $true) }
      }
    } finally { $zip.Dispose() }
  } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
  # Like Astral's own installer, put the folder on the PATH for new terminals (uv's own logic: it keeps the registry value intact)
  $env:PATH = "$localBin;$env:PATH"
  N { uv tool update-shell } | Out-Null
}

# A step that ran a program and failed: say what git, uv or whatever printed (the last lines of it), not a guess about why.
function Stop-WithOutput([string]$What, $Output, [string]$Hint = '') {
  Write-Host "[claude-hr-install] ERROR: $What" -ForegroundColor Red
  $tail = @($Output | ForEach-Object { "$_".Trim() } | Where-Object { $_ } | Select-Object -Last 6)
  if ($tail.Count) { Write-Host "  it printed:" -ForegroundColor Red; foreach ($line in $tail) { Write-Host "    $line" -ForegroundColor Red } }
  else { Write-Host '  it printed nothing' -ForegroundColor Red }
  if ($Hint) { Write-Host $Hint -ForegroundColor Red }
  throw "claude-hr-install failed: $What (details above)"
}

# What to tell a person whose PC will not start a program that is there. $Extra: anything that is specific to the program.
function Get-BlockedAdvice([string]$Path, [string]$Extra = '') {
  ("A program that is installed but will not start is being blocked on this PC: antivirus, AppLocker or other application control (ThreatLocker, Defender rules, a policy of your organisation). " +
    "After a product like that is removed, restart Windows before trying again: it may keep blocking until then. " +
    "In a normal PowerShell window run   & '$($Path -replace "'", "''")' --version   to see Windows' exact reason, and ask whoever manages this PC to allow that file. $Extra").Trim()
}

# Try each route until `$Command` runs. A route fails by throwing; every reason is kept for the final message.
function Install-Tool([string]$Command, [hashtable[]]$Routes, [string]$Manual) {
  $why = @(); $blocked = $null
  foreach ($route in $Routes) {
    $threw = $false
    try { & $route.Run } catch { $threw = $true; $why += "$($route.Name): $($_.Exception.Message)" }
    Update-SessionPath
    if (Test-HaveCommand $Command) {
      $run = Test-ToolRuns $Command
      if ($run.Ok) { return }
      $blocked = $run  # found is not the same as working: keep where it is and what Windows said
      $why += "$($route.Name): installed at $($run.Path), but it will not run: $($run.Error)"
    } elseif (-not $threw) { $why += "$($route.Name): finished, but '$Command' is still not on the PATH" }
    Write-Host "[claude-hr-install] $($why[-1])" -ForegroundColor Yellow
  }
  Show-Reachability
  Write-Host "[claude-hr-install] ERROR: could not install $Command. What was tried:" -ForegroundColor Red
  foreach ($w in $why) { Write-Host "  - $w" -ForegroundColor Red }
  if ($blocked) { Show-BlockEvidence $blocked.Path; Write-Host (Get-BlockedAdvice $blocked.Path) -ForegroundColor Red }
  Write-Host $Manual -ForegroundColor Red
  throw "claude-hr-install failed: could not install $Command (details above)"
}

function Install-Uv {
  Log 'installing uv'
  Install-Tool 'uv' @(
    @{ Name = 'astral.sh installer'; Run = { Invoke-DownloadedScript 'https://astral.sh/uv/install.ps1' } },
    @{ Name = 'GitHub release'; Run = { Install-UvFromGitHub } },
    @{ Name = 'winget'; Run = { Install-WithWinget 'astral-sh.uv' } }
  ) 'Install uv another way (https://docs.astral.sh/uv/getting-started/installation/), open a NEW terminal and run this installer again.'
}

function Install-ClaudeCode {
  Log 'installing Claude Code (native build)'
  Install-Tool 'claude' @(
    @{ Name = 'claude.ai installer'; Run = { Invoke-DownloadedScript 'https://claude.ai/install.ps1' } },
    @{ Name = 'winget'; Run = { Install-WithWinget 'Anthropic.ClaudeCode' } }
  ) 'Install Claude Code another way (https://code.claude.com/docs/en/setup), open a NEW terminal and run this installer again.'
}
# One that was just installed has been checked already. One that was there before is the person's own business: a Claude Code
# that will not run is a warning (claude-hr installs anyway), never a crash of this script.
function Show-ClaudeCode {
  $run = Test-ToolRuns claude
  if ($run.Ok) { Log ("claude " + $run.Version) }
  else { Write-Host "[claude-hr-install] WARNING: Claude Code ($($run.Path)) did not run: $($run.Error). claude-hr is installed anyway, but Claude Code itself needs attention." -ForegroundColor Yellow }
}
# What this PC itself recorded about a program it would not start (read-only; a log that cannot be read, or a cmdlet that is not
# there, is skipped). Returns lines of text: a Deny rule on the file or its folder, security software that is present, and the
# events of the last day in the logs of Defender, code integrity (WDAC, Smart App Control) and AppLocker that name the file.
function Get-BlockEvidence([string]$Path) {
  $found = New-Object System.Collections.ArrayList
  $leaf = Split-Path -Leaf $Path
  foreach ($target in @($Path, (Split-Path -Parent $Path))) {
    try {
      foreach ($rule in @((Get-Acl -LiteralPath $target -ErrorAction Stop).Access)) {
        if ("$($rule.AccessControlType)" -eq 'Deny') { [void]$found.Add("$target has a Deny rule for $($rule.IdentityReference) ($($rule.FileSystemRights))") }
      }
    } catch { }
  }
  try {
    $products = 'ThreatLocker|SentinelOne|SentinelAgent|CrowdStrike|CSFalcon|Carbon Black|CbDefense|Cylance|Sophos|Symantec|McAfee|Trend Micro|Kaspersky|ESET|Bitdefender|Malwarebytes|Webroot|Cortex XDR|Elastic Endpoint|Application Identity|Advanced Threat Protection'
    $names = @(Get-Service -ErrorAction Stop | Where-Object { "$($_.Name) $($_.DisplayName)" -match $products } | ForEach-Object { "$($_.DisplayName) ($($_.Status))" })
    if ($names.Count) { [void]$found.Add('security software that decides what may run is present: ' + ($names -join ', ')) }
  } catch { }
  try {
    foreach ($dir in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData)) {
      if ($dir -and (Test-Path -LiteralPath (Join-Path $dir 'ThreatLocker'))) {
        [void]$found.Add("ThreatLocker is still on this PC ($dir\ThreatLocker exists): removing it is not finished until Windows has been restarted")
        break
      }
    }
  } catch { }
  $since = (Get-Date).AddDays(-1)
  $logs = @(
    @{ Log = 'Microsoft-Windows-Windows Defender/Operational'; Ids = @(1121, 1122, 1116, 1117); Who = 'Microsoft Defender' },
    @{ Log = 'Microsoft-Windows-CodeIntegrity/Operational'; Ids = @(3033, 3034, 3076, 3077); Who = 'Windows code integrity (WDAC or Smart App Control)' },
    @{ Log = 'Microsoft-Windows-AppLocker/EXE and DLL'; Ids = @(8003, 8004); Who = 'AppLocker' }
  )
  foreach ($l in $logs) {
    try {
      $hit = Get-WinEvent -FilterHashtable @{ LogName = $l.Log; Id = $l.Ids; StartTime = $since } -MaxEvents 300 -ErrorAction Stop |
        Where-Object { "$($_.Message)" -like ('*' + [WildcardPattern]::Escape($leaf) + '*') } | Select-Object -First 1
      if ($hit) {
        $text = ("$($hit.Message)" -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 4 | ForEach-Object { $_.Trim() }) -join ' | '
        $rule = if ("$($hit.Message)" -match '01443614-cd74-433a-b99e-2ecdc07bfc25') { ' = "Block executable files from running unless they meet a prevalence, age, or trusted list criterion"' } else { '' }
        [void]$found.Add("$($l.Who), event $($hit.Id) at $($hit.TimeCreated.ToString('yyyy-MM-dd HH:mm')): $text$rule")
      }
    } catch { }
  }
  $found
}

function Show-BlockEvidence([string]$Path) {
  $lines = @(Get-BlockEvidence $Path)
  $leaf = if ($Path) { Split-Path -Leaf $Path } else { 'the program' }
  Write-Host "  What this PC records about $($leaf):" -ForegroundColor Red
  if ($lines.Count) { foreach ($l in $lines) { Write-Host "    - $l" -ForegroundColor Red } }
  else { Write-Host '    - nothing: no rule on the file, and no Defender, code integrity or AppLocker event names it. The product that blocks it keeps its own log - look in its console.' -ForegroundColor Red }
}

# The Python of claude-hr's tool environment: what claude-hr.exe and claude-hr-ctl.exe start, and enough to run claude-hr when
# Windows will not start those launchers. '' when it is not there.
function Get-ToolPython {
  $dir = (N { uv tool dir } | Select-Object -First 1)
  if (-not $dir) { return '' }
  $py = Join-Path "$dir" 'claude-hr\Scripts\python.exe'
  if (Test-Path -LiteralPath $py) { $py } else { '' }
}

# claude-hr's commands are launcher programs (.exe) that uv writes when it installs the tool: new files that nobody has run,
# which endpoint security and application control may refuse ("Access is denied") while the Python interpreter runs. So the
# first thing run is claude-hr-ctl; if Windows refuses it, setup is run through the Python that it would have started, and
# its "launchers" step puts stand-ins (.cmd files) where the refused launchers are. Returns @{ ViaPython; Python }.
function Resolve-HrCtl {
  $run = Test-ToolRuns claude-hr-ctl
  if ($run.Ok) { return @{ ViaPython = $false; Python = '' } }
  Write-Host "[claude-hr-install] WARNING: claude-hr-ctl was installed at $($run.Path) but does not start: $($run.Error)" -ForegroundColor Yellow
  Show-BlockEvidence $run.Path
  $py = Get-ToolPython
  $pyRun = if ($py) { Test-ToolRuns $py } else { @{ Ok = $false; Path = ''; Error = 'the Python of the claude-hr tool environment was not found' } }
  if ($pyRun.Ok) {
    Log "Python ($py) starts, so claude-hr is set up through it; setup puts stand-ins where the launcher programs are refused"
    return @{ ViaPython = $true; Python = $py }
  }
  $where = if ($py) { $py } else { $run.Path }
  Write-Host "[claude-hr-install] ERROR: Windows will not start claude-hr's Python either: $($pyRun.Error)" -ForegroundColor Red
  Write-Host (Get-BlockedAdvice $where "claude-hr's launcher programs are in $(if ($run.Path) { Split-Path -Parent $run.Path } else { $localBin }) and its Python environment is under %APPDATA%\uv\tools: a rule has to allow both folders. Run this installer again afterwards (it skips what is done).") -ForegroundColor Red
  throw 'claude-hr-install failed: Windows will not start claude-hr or its Python (details above)'
}

# After setup: what a person types has to start. $true when both commands do.
function Test-HrCommandsRun {
  $ok = $true
  foreach ($name in 'claude-hr-ctl', 'claude-hr') {
    $run = Test-ToolRuns $name
    if (-not $run.Ok) { $ok = $false; Write-Host "[claude-hr-install] WARNING: $name does not start: $($run.Error)" -ForegroundColor Yellow }
  }
  $ok
}

# Programs of claude-hr that are running: its launchers, and anything that runs from the tool environment (the Python of a
# session started through a stand-in has no claude-hr name, and it keeps uv from replacing the environment).
function Get-ClaudeHrProcesses {
  $found = @(Get-Process -Name 'claude-hr', 'claude-hr-ctl' -ErrorAction SilentlyContinue)
  $dir = (N { uv tool dir } | Select-Object -First 1)
  if ($dir) {
    $inEnv = (Join-Path "$dir" 'claude-hr') + '\'
    $found += @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
      $p = $null; try { $p = $_.Path } catch { }
      $p -and $_.Id -ne $PID -and $p.StartsWith($inEnv, [StringComparison]::OrdinalIgnoreCase)
    })
  }
  @($found | Sort-Object Id -Unique)
}

function Show-Done([bool]$ViaPython, [bool]$CommandsRun, [string]$Python) {
  if ($ViaPython -and $CommandsRun) {
    Write-Host @"

claude-hr is installed - with stand-ins for its launcher programs.
  Windows refuses to start claude-hr.exe and its siblings on this PC ($localBin), so setup moved the refused ones aside and put
  small stand-ins there (claude-hr.cmd and a shell script of the same name) that start the same commands through Python.
  It works like the real thing, with three differences:
    - Headroom (token compression) was not installed: headroom.exe comes from the same tool and would be refused too.
    - In PowerShell and cmd.exe the arguments of claude-hr go through cmd.exe, which acts on & | < > and % ^ in them: never give
      claude-hr text from an untrusted source that way (a script calls Python directly:
      $Python -I -m claude_hr.launcher ...). Git Bash is not affected. To keep only the Git Bash scripts:
      claude-hr-ctl config set launchers.cmd false
    - After an update, if Windows still refuses the new launchers, setup puts the stand-ins back by itself.
  To get the real launchers: have $localBin and %APPDATA%\uv\tools allowed by whoever manages this PC's security
  software, then run   claude-hr-ctl launchers ensure
  Open a NEW terminal, then:   claude-hr
  Health:                      claude-hr-ctl doctor
  First run on a new machine: log in to Claude Code (/login); until then claude-hr starts it without Headroom on purpose.
"@
    return
  }
  if ($ViaPython) {
    Write-Host @"

claude-hr is installed, but its commands do not start from the command line yet (see the warnings above).
  What does work, through Python:
    & '$($Python -replace "'", "''")' -I -m claude_hr.launcher          (claude-hr)
    & '$($Python -replace "'", "''")' -I -m claude_hr.ctl doctor       (claude-hr-ctl)
  Fix the cause above, restart Windows if a security product was removed, then run this installer again.
"@
    return
  }
  Write-Host @"

claude-hr is installed.
  Open a NEW terminal, then:   claude-hr
                               claude-hr agents --dangerously-skip-permissions
  Health:                      claude-hr-ctl doctor
  If 'claude-hr' is not found: run  uv tool update-shell  and restart the terminal.
  First run on a new machine: log in to Claude Code (/login); until then claude-hr starts it without Headroom on purpose.
"@
  if (-not $CommandsRun) { Write-Host "[claude-hr-install] WARNING: claude-hr-ctl or claude-hr did not start when it was tried above: run  claude-hr-ctl doctor  in a new terminal." -ForegroundColor Yellow }
}
# ---- downloads: END -------------------------------------------------------------------------------------------------------

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die 'git is required: winget install --id Git.Git -e' }

$localBin = Join-Path $env:USERPROFILE '.local\bin'
if ($env:PATH -notlike "*$localBin*") { $env:PATH = "$localBin;$env:PATH" }

# ---- uv ----------------------------------------------------------------------------------
if (-not (Test-HaveCommand uv)) { Install-Uv }

# ---- Claude Code -------------------------------------------------------------------------
if (-not (Test-HaveCommand claude)) { Install-ClaudeCode }
Show-ClaudeCode

# ---- claude-hr source -------------------------------------------------------------------------
$hrHome = if ($env:CLAUDE_HR_HOME) { $env:CLAUDE_HR_HOME } else { Join-Path $env:USERPROFILE '.claude-hr' }
New-Item -ItemType Directory -Force -Path $hrHome | Out-Null
$src = Join-Path $hrHome 'repo'
if ($Local) {
  $Local = (Resolve-Path $Local).Path
  if ($Local -ne $src) {
    Log "using checkout $Local"
    if (Test-Path $src) { Remove-Item -Recurse -Force $src }
    # copy the working tree as it is on disk (committed or not)
    robocopy $Local $src /E /XD .git __pycache__ .venv .pytest_cache .ruff_cache /XF .git /NFL /NDL /NJH /NJS /NP | Out-Null  # /XF .git: a git worktree has a .git FILE that would point the copy at the original repository
    if ($LASTEXITCODE -ge 8) { Die "copy failed (robocopy $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
  }
} else {
  if (-not $Repo) { Die 'no repo URL: pass -Repo <url>, set $env:CLAUDE_HR_REPO_URL, or use -Local <path>' }
  # The token travels in git's ENVIRONMENT (GIT_CONFIG_*), never in argv, so it is not visible in process listings.
  if ($env:CLAUDE_HR_GIT_TOKEN) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$($env:CLAUDE_HR_GIT_TOKEN)"))
    $env:GIT_CONFIG_COUNT = '1'; $env:GIT_CONFIG_KEY_0 = 'http.extraheader'; $env:GIT_CONFIG_VALUE_0 = "Authorization: Basic $b64"
  }
  # A copy left by an earlier -Local install has no .git: replace it, `git clone` refuses a non-empty directory.
  if ((Test-Path $src) -and -not (Test-Path (Join-Path $src '.git'))) {
    Log "replacing the non-git copy at $src (left by a -Local install)"
    Remove-Item -Recurse -Force $src
  }
  $repoExplicit = $PSBoundParameters.ContainsKey('Repo') -or [bool]$env:CLAUDE_HR_REPO_URL
  try {
    if (Test-Path (Join-Path $src '.git')) {
      Log "updating $src"
      if ($repoExplicit) {
        $curUrl = (N { git -C $src remote get-url origin } | Select-Object -First 1)
        if ("$curUrl".Trim() -ne $Repo) { Log "switching origin to $Repo"; N { git -C $src remote set-url origin $Repo } | Out-Null }
      }
      $gitOut = @(N { git -C $src fetch --quiet --tags --prune origin })
    } else {
      Log "cloning $Repo ($Ref)"
      $gitOut = @(N { git clone --quiet $Repo $src })
    }
    if ($LASTEXITCODE -ne 0) {
      Stop-WithOutput 'git could not fetch the claude-hr repository' $gitOut ('Check the URL, your network and credentials. If git says "dubious ownership", run this installer in a normal (not Administrator) PowerShell window ' +
        'of the account that installed it the first time; git''s own message above names the exact "git config --global --add safe.directory" command if you must keep using this window.')
    }
  } finally {
    Remove-Item Env:GIT_CONFIG_COUNT, Env:GIT_CONFIG_KEY_0, Env:GIT_CONFIG_VALUE_0 -ErrorAction SilentlyContinue
  }
  # Native-command stderr becomes a terminating error under $ErrorActionPreference='Stop' in Windows PowerShell 5.1,
  # so probe the ref with Continue and decide from the exit code.
  # Prefer the remote-tracking ref: a LOCAL branch `main` is only as new as the original clone. Tags and SHAs fall through.
  $gitOut = @(N { git -C $src checkout --quiet --detach "origin/$Ref" })
  if ($LASTEXITCODE -ne 0) { $gitOut = @(N { git -C $src checkout --quiet --detach $Ref }) }
  if ($LASTEXITCODE -ne 0) { Stop-WithOutput "could not check out '$Ref'" $gitOut "Is '$Ref' a branch, tag or commit of $Repo?" }
}

# Replacing the tool environment under a running launcher fails on Windows and can leave a broken install.
$busy = @(Get-ClaudeHrProcesses)
if ($busy.Count -and -not $env:CLAUDE_HR_FORCE_INSTALL) {
  Die ("claude-hr is running (" + (($busy | Select-Object -First 3 | ForEach-Object { "$($_.ProcessName) $($_.Id)" }) -join ', ') + "). Close those sessions and re-run, or set `$env:CLAUDE_HR_FORCE_INSTALL=1.")
}

# ---- commands ----------------------------------------------------------------------------
Log 'installing claude-hr and claude-hr-ctl'
$uvOut = @(N { uv tool install --python 3.13 --force --reinstall $src })
if ($LASTEXITCODE -ne 0) { Stop-WithOutput 'uv tool install failed' $uvOut 'uv needs pypi.org, files.pythonhosted.org and github.com (it downloads Python 3.13 from there if the PC has none).' }
$toolBin = (N { uv tool dir --bin } | Select-Object -First 1)
if ($toolBin -and ($env:PATH -notlike "*$toolBin*")) { $env:PATH = "$toolBin;$env:PATH" }
if (-not (Get-Command claude-hr-ctl -ErrorAction SilentlyContinue)) { Die 'claude-hr-ctl not on PATH after install (try: uv tool update-shell)' }
$hrCtl = Resolve-HrCtl

$setupArgs = @('setup')
if ($PluginProfile) { $setupArgs += @('--profile', $PluginProfile) }
if ($NoHeadroomInstall) { $setupArgs += '--no-headroom-install' }
if ($SkipSchedule) { $setupArgs += @('--skip', 'schedule') }
if ($DryRun) { $setupArgs += '--dry-run' }
Log ("running claude-hr-ctl " + ($setupArgs -join ' ') + $(if ($hrCtl.ViaPython) { ' (through Python)' }))
$env:CLAUDE_HR_REPO = $src
if ($hrCtl.ViaPython) { N { & $hrCtl.Python -I -m claude_hr.ctl @setupArgs } } else { N { claude-hr-ctl @setupArgs } }
if ($LASTEXITCODE -ne 0) { Write-Host '[claude-hr-install] setup reported problems - see above' -ForegroundColor Yellow }

if ($Shim) {
  if ($hrCtl.ViaPython) { Write-Host '[claude-hr-install] -Shim skipped: a claude.exe copy of the refused claude-hr.exe would be refused too (claude-hr-ctl shim install says more).' -ForegroundColor Yellow }
  else { N { claude-hr-ctl shim install } }
}

Show-Done $hrCtl.ViaPython (Test-HrCommandsRun) $hrCtl.Python
