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

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die 'git is required: winget install --id Git.Git -e' }

$localBin = Join-Path $env:USERPROFILE '.local\bin'
if ($env:PATH -notlike "*$localBin*") { $env:PATH = "$localBin;$env:PATH" }

# ---- uv ----------------------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Log 'installing uv'
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:PATH = "$localBin;$env:PATH"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv install failed' }
}

# ---- Claude Code -------------------------------------------------------------------------
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
  Log 'installing Claude Code (native build)'
  Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
  $env:PATH = "$localBin;$env:PATH"
  if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Die 'Claude Code install failed; see https://code.claude.com/docs/en/setup' }
}
Log ("claude " + ((N { claude --version }) | Select-Object -First 1))

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
      N { git -C $src fetch --quiet --tags --prune origin } | Out-Null
    } else {
      Log "cloning $Repo ($Ref)"
      N { git clone --quiet $Repo $src } | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { Die 'git could not fetch the claude-hr repository (check the URL and credentials)' }
  } finally {
    Remove-Item Env:GIT_CONFIG_COUNT, Env:GIT_CONFIG_KEY_0, Env:GIT_CONFIG_VALUE_0 -ErrorAction SilentlyContinue
  }
  # Native-command stderr becomes a terminating error under $ErrorActionPreference='Stop' in Windows PowerShell 5.1,
  # so probe the ref with Continue and decide from the exit code.
  # Prefer the remote-tracking ref: a LOCAL branch `main` is only as new as the original clone. Tags and SHAs fall through.
  N { git -C $src checkout --quiet --detach "origin/$Ref" } | Out-Null
  if ($LASTEXITCODE -ne 0) { N { git -C $src checkout --quiet --detach $Ref } | Out-Null }
  if ($LASTEXITCODE -ne 0) { Die "could not check out '$Ref'" }
}

# Replacing the tool environment under a running launcher fails on Windows and can leave a broken install.
$busy = Get-Process -Name 'claude-hr', 'claude-hr-ctl' -ErrorAction SilentlyContinue
if ($busy -and -not $env:CLAUDE_HR_FORCE_INSTALL) {
  Die ("claude-hr is running (PID " + (($busy | Select-Object -First 3).Id -join ', ') + "). Close those sessions and re-run, or set `$env:CLAUDE_HR_FORCE_INSTALL=1.")
}

# ---- commands ----------------------------------------------------------------------------
Log 'installing claude-hr and claude-hr-ctl'
N { uv tool install --python 3.13 --force --reinstall $src } | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'uv tool install failed' }
$toolBin = (N { uv tool dir --bin } | Select-Object -First 1)
if ($toolBin -and ($env:PATH -notlike "*$toolBin*")) { $env:PATH = "$toolBin;$env:PATH" }
if (-not (Get-Command claude-hr-ctl -ErrorAction SilentlyContinue)) { Die 'claude-hr-ctl not on PATH after install (try: uv tool update-shell)' }

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
