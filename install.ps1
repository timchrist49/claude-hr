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
    } catch { $why = Get-ErrorReason $_.Exception.Message }
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
  if ($blocked) { Write-Host (Get-BlockedAdvice $blocked.Path) -ForegroundColor Red }
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
# claude-hr's own commands are launcher programs (.exe) that uv writes next to the tool environment, so they are new files that
# security software has never seen; one that will not start must stop the install here, with the reason, not crash in setup.
function Assert-HrCtlRuns {
  $run = Test-ToolRuns claude-hr-ctl
  if ($run.Ok) { return }
  Write-Host "[claude-hr-install] ERROR: claude-hr-ctl was installed at $($run.Path) but will not start: $($run.Error)" -ForegroundColor Red
  $folder = if ($run.Path) { Split-Path -Parent $run.Path } else { $localBin }
  Write-Host (Get-BlockedAdvice $run.Path "claude-hr's commands are small launcher programs that uv creates in $folder, and their Python environment is under %APPDATA%\uv\tools: a rule has to allow both folders. Run this installer again afterwards (it skips what is done).") -ForegroundColor Red
  throw 'claude-hr-install failed: claude-hr-ctl will not start (details above)'
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
$busy = Get-Process -Name 'claude-hr', 'claude-hr-ctl' -ErrorAction SilentlyContinue
if ($busy -and -not $env:CLAUDE_HR_FORCE_INSTALL) {
  Die ("claude-hr is running (PID " + (($busy | Select-Object -First 3).Id -join ', ') + "). Close those sessions and re-run, or set `$env:CLAUDE_HR_FORCE_INSTALL=1.")
}

# ---- commands ----------------------------------------------------------------------------
Log 'installing claude-hr and claude-hr-ctl'
$uvOut = @(N { uv tool install --python 3.13 --force --reinstall $src })
if ($LASTEXITCODE -ne 0) { Stop-WithOutput 'uv tool install failed' $uvOut 'uv needs pypi.org, files.pythonhosted.org and github.com (it downloads Python 3.13 from there if the PC has none).' }
$toolBin = (N { uv tool dir --bin } | Select-Object -First 1)
if ($toolBin -and ($env:PATH -notlike "*$toolBin*")) { $env:PATH = "$toolBin;$env:PATH" }
if (-not (Get-Command claude-hr-ctl -ErrorAction SilentlyContinue)) { Die 'claude-hr-ctl not on PATH after install (try: uv tool update-shell)' }
Assert-HrCtlRuns

$setupArgs = @('setup')
if ($PluginProfile) { $setupArgs += @('--profile', $PluginProfile) }
if ($NoHeadroomInstall) { $setupArgs += '--no-headroom-install' }
if ($SkipSchedule) { $setupArgs += @('--skip', 'schedule') }
if ($DryRun) { $setupArgs += '--dry-run' }
Log ("running claude-hr-ctl " + ($setupArgs -join ' '))
$env:CLAUDE_HR_REPO = $src
N { claude-hr-ctl @setupArgs }
if ($LASTEXITCODE -ne 0) { Write-Host '[claude-hr-install] setup reported problems - see above' -ForegroundColor Yellow }

if ($Shim) { N { claude-hr-ctl shim install } }

Write-Host @"

claude-hr is installed.
  Open a NEW terminal, then:   claude-hr
                               claude-hr agents --dangerously-skip-permissions
  Health:                      claude-hr-ctl doctor
  If 'claude-hr' is not found: run  uv tool update-shell  and restart the terminal.
  First run on a new machine: log in to Claude Code (/login); until then claude-hr starts it without Headroom on purpose.
"@
