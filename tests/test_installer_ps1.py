"""install.ps1: a download that cannot be made must not end the install.

The download helpers sit between two markers in install.ps1. These tests cut that block out and run it under the real
PowerShell with the network (Invoke-WebRequest), winget and uv replaced by functions, so every route and every failure
message is exercised without touching the network, the registry or the real uv.
"""

import base64
import hashlib
import http.server
import os
import re
import shutil
import socket
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.ps1"
START = "$ErrorActionPreference = 'Stop'"
END = "# ---- downloads: END"

ASTRAL = "https://astral.sh/uv/install.ps1"
ZIP = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
CLAUDE = "https://claude.ai/install.ps1"

windows_only = pytest.mark.skipif(os.name != "nt", reason="install.ps1 is a Windows installer")


def _shell():
    return shutil.which("powershell") or shutil.which("pwsh")


def _block() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    assert START in text and END in text, (
        "the download block markers in install.ps1 changed: update this test"
    )
    start = text.index(START)
    end = text.index("\n", text.index(END))
    return text[start:end]


def q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


PRELUDE = """
$localBin = {bin}
$env:PROCESSOR_ARCHITEW6432 = 'AMD64'
$global:Calls = New-Object System.Collections.ArrayList
$global:Have = @()
$global:Web = @{{}}
$global:WingetExit = 0
$global:WingetProvides = $null
$global:Broken = @()
$global:FakeDir = 'C:\\fake'
"""

# Defined AFTER the block, so they replace its commands; functions win over cmdlets and programs.
MOCKS = """
function Start-Sleep { }
function Test-HaveCommand([string]$Name) {
  ($global:Have -contains $Name) -or (Test-Path -LiteralPath (Join-Path $localBin "$Name.exe"))
}
function Invoke-WebRequest {
  [CmdletBinding()]
  param([string]$Uri, [string]$OutFile, [string]$Method = 'Get', [switch]$UseBasicParsing, [int]$TimeoutSec)
  $global:Calls.Add("$Method $Uri") | Out-Null
  $h = $global:Web[$Uri]
  if ($null -eq $h) { throw 'Unable to connect to the remote server' }
  & $h $OutFile
}
function winget {
  $global:Calls.Add("winget $args") | Out-Null
  $global:LASTEXITCODE = $global:WingetExit
  if ($global:WingetExit -eq 0 -and $global:WingetProvides) { $global:Have += $global:WingetProvides }
}
function uv { $global:Calls.Add("uv $args") | Out-Null; $global:LASTEXITCODE = 0 }
function Test-ToolRuns([string]$Name) {
  if ($global:Broken -contains $Name) {
    return @{ Ok = $false; Path = "$($global:FakeDir)\\$Name.exe"; Version = ''; Error = "Program '$Name.exe' failed to run: Access is denied" }
  }
  @{ Ok = $true; Path = "$($global:FakeDir)\\$Name.exe"; Version = '9.9.9'; Error = '' }
}
"""


def run_block(tmp_path, scenario: str, *, mocks=True, setup="", pre="") -> dict:
    """Run `scenario` (PowerShell) after the installer's download block. Returns the output, the result line and the calls."""
    shell = _shell()
    if shell is None:
        pytest.skip("needs PowerShell")
    bin_dir = tmp_path / "bin"
    script = (
        PRELUDE.format(bin=q(bin_dir))
        + pre
        + _block()
        + "\n"
        + (MOCKS if mocks else "")
        + setup
        + "\ntry { "
        + scenario
        + " ; 'RESULT=OK' } catch { 'RESULT=THROW: ' + $_.Exception.Message }\n"
        + "foreach ($c in $global:Calls) { 'CALL ' + $c }\n"
    )
    ps1 = tmp_path / "scenario.ps1"
    ps1.write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))
    # The installer stages its downloads in %LOCALAPPDATA%\Temp: that, TEMP and the WinGet folder are the test's own, and
    # whatever a scenario leaves in the staging folder is a failure of its cleanup.
    staging = tmp_path / "local" / "Temp"
    staging.mkdir(parents=True)
    (tmp_path / "tmp").mkdir()
    env = {
        **os.environ,
        "LOCALAPPDATA": str(tmp_path / "local"),
        "TEMP": str(tmp_path / "tmp"),
        "TMP": str(tmp_path / "tmp"),
    }
    out = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    ).stdout
    leftovers = sorted(p.name for p in staging.iterdir())
    assert not leftovers, f"download staging folders left behind: {leftovers}\n{out}"
    lines = out.splitlines()
    return {
        "out": out,
        "result": next((ln for ln in lines if ln.startswith("RESULT=")), "NO RESULT LINE\n" + out),
        "calls": [ln[5:] for ln in lines if ln.startswith("CALL ")],
        "bin": bin_dir,
    }


def script_act(text: str) -> str:
    """What a fake download does to write `text` (the body of an installer script) to the file it is asked for."""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"[IO.File]::WriteAllBytes($f, [Convert]::FromBase64String('{b64}'))"


def fake_uv(bin_dir) -> str:
    """An installer that works: prints a line and puts uv.exe where the real one would."""
    return (
        "Write-Output 'downloading uv (fake)'\n"
        f"New-Item -ItemType Directory -Force -Path {q(bin_dir)} | Out-Null\n"
        f"Set-Content -LiteralPath {q(Path(bin_dir) / 'uv.exe')} -Value 'stub'\n"
    )


def web(entries: dict) -> str:
    """$global:Web: url -> what the fake download does with the file it is asked to write."""
    body = "\n".join(f"  {q(url)} = {{ param($f) {act} }}" for url, act in entries.items())
    return "$global:Web = @{\n" + body + "\n}\n"


def make_zip(tmp_path, name="uv.zip") -> tuple[Path, str]:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for exe in ("uv.exe", "uvx.exe", "uvw.exe"):
            z.writestr(exe, b"MZ stub of " + exe.encode())
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def serve_zip(tmp_path, *, checksum=None) -> str:
    """The two GitHub downloads of the zip route, as web() entries."""
    zpath, digest = make_zip(tmp_path)
    sha = checksum or digest
    return web(
        {
            ZIP: f"Copy-Item -LiteralPath {q(zpath)} -Destination $f -Force",
            ZIP
            + ".sha256": f"Set-Content -LiteralPath $f -Value '{sha}  uv-x86_64-pc-windows-msvc.zip'",
        }
    )


# ---- the file itself -------------------------------------------------------------------------------------------------
def test_installer_is_plain_ascii_so_no_shell_or_encoding_can_misread_it():
    # `irm | iex` decodes the download; Windows PowerShell 5.1 reads a local file without a BOM as ANSI
    data = INSTALLER.read_bytes()
    assert not [b for b in data if b > 127], "install.ps1 must stay ASCII"
    assert b"\r" not in data


@windows_only
def test_installer_parses_with_the_real_powershell_parser():
    shell = _shell()
    if shell is None:
        pytest.skip("needs PowerShell")
    check = (
        "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"{q(INSTALLER)}, [ref]$t, [ref]$e); if ($e.Count -eq 0) {{ 'PARSE_OK' }} else {{ $e | ForEach-Object {{ $_.Message }} }}"
    )
    out = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert "PARSE_OK" in out, out


def test_uv_and_claude_code_are_installed_only_when_missing_and_through_the_helpers():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "if (-not (Test-HaveCommand uv)) { Install-Uv }" in text
    assert "if (-not (Test-HaveCommand claude)) { Install-ClaudeCode }" in text
    # the single-shot downloads that failed with no way out are gone
    assert not re.search(r"Invoke-RestMethod\s+https?://", text)
    # downloaded installers must never run through Invoke-Expression in the person's own session: they end with `exit 1`
    code = [ln for ln in _block().splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if re.search(r"Invoke-Expression|\biex\b", ln)], code
    # the signed-in user's credentials are not offered to whatever proxy answers (a rogue WPAD proxy could harvest them)
    assert "DefaultNetworkCredentials" not in text and ".Credentials" not in text


# ---- the routes ------------------------------------------------------------------------------------------------------
@windows_only
def test_uv_comes_from_astralsh_when_it_answers(tmp_path):
    r = run_block(
        tmp_path, "Install-Uv", setup=web({ASTRAL: script_act(fake_uv(tmp_path / "bin"))})
    )
    assert r["result"] == "RESULT=OK", r["out"]
    assert r["calls"] == [f"Get {ASTRAL}"]
    assert "  downloading uv (fake)" in r["out"], "what the installer printed was not shown"


@windows_only
def test_downloads_are_staged_in_the_users_own_folder_and_start_with_a_bom(tmp_path):
    """Not in a TEMP that may be shared (what is checked there could be swapped before it runs); the script file carries a BOM
    so that Windows PowerShell 5.1 does not read non-ASCII text as ANSI."""
    peek = "Write-Output ('HEAD=' + (([IO.File]::ReadAllBytes($PSCommandPath)[0..2]) -join ','))\n"
    act = "$global:Seen = $f; " + script_act(peek + fake_uv(tmp_path / "bin"))
    r = run_block(
        tmp_path,
        "Install-Uv; 'SEEN=' + $global:Seen",
        setup=web({ASTRAL: act}),
    )
    assert r["result"] == "RESULT=OK", r["out"]
    seen = next(ln for ln in r["out"].splitlines() if ln.startswith("SEEN="))[5:]
    assert seen.startswith(str(tmp_path / "local" / "Temp") + os.sep + "claude-hr-"), seen
    assert "HEAD=239,187,191" in r["out"]


@windows_only
def test_uv_comes_from_its_github_release_when_astralsh_cannot_be_reached(tmp_path):
    """The reported failure: 'Unable to connect to the remote server' for astral.sh."""
    r = run_block(tmp_path, "Install-Uv", setup=serve_zip(tmp_path))
    assert r["result"] == "RESULT=OK", r["out"]
    assert [c for c in r["calls"] if "astral.sh" in c] == [
        f"Get {ASTRAL}"
    ] * 2  # two attempts, then the next route
    assert f"Get {ZIP}" in r["calls"] and f"Get {ZIP}.sha256" in r["calls"]
    assert sorted(p.name for p in r["bin"].iterdir()) == ["uv.exe", "uvw.exe", "uvx.exe"]
    assert (
        "uv tool update-shell" in r["calls"]
    )  # new terminals find it, as after Astral's own installer
    assert not any(c.startswith("winget") for c in r["calls"])


@windows_only
def test_the_github_route_needs_no_module_that_windows_powershell_loads_on_first_use(tmp_path):
    """Get-FileHash and Expand-Archive are script functions of modules that Windows PowerShell 5.1 loads on first use. On the
    CI runners that load failed ("The term 'Get-FileHash' is not recognized"), taking the whole route down. These two stand-ins
    make exactly those commands unavailable on any machine; the installer hashes and unpacks with .NET instead."""
    gone = "function {0} {{ throw \"The term '{0}' is not recognized as the name of a cmdlet\" }}\n"
    r = run_block(
        tmp_path,
        "Install-Uv",
        setup=serve_zip(tmp_path),
        pre=gone.format("Get-FileHash") + gone.format("Expand-Archive"),
    )
    assert r["result"] == "RESULT=OK", r["out"]
    assert sorted(p.name for p in r["bin"].iterdir()) == ["uv.exe", "uvw.exe", "uvx.exe"]
    assert "not recognized" not in r["out"]


@windows_only
def test_only_the_three_uv_programs_are_taken_out_of_the_zip(tmp_path):
    """Whatever else a zip holds (other names, a folder path that climbs out) is never written."""
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("uv.exe", b"MZ uv")
        z.writestr("sub/uvx.exe", b"MZ uvx")
        z.writestr("../escaped.exe", b"MZ no")
        z.writestr("readme.txt", b"no")
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    setup = web(
        {
            ZIP: f"Copy-Item -LiteralPath {q(zpath)} -Destination $f -Force",
            ZIP
            + ".sha256": f"Set-Content -LiteralPath $f -Value '{digest}  uv-x86_64-pc-windows-msvc.zip'",
        }
    )
    r = run_block(tmp_path, "Install-Uv", setup=setup)
    assert r["result"] == "RESULT=OK", r["out"]
    assert sorted(p.name for p in r["bin"].iterdir()) == ["uv.exe", "uvx.exe"]
    assert not (tmp_path / "escaped.exe").exists()


@windows_only
def test_a_zip_that_does_not_match_its_checksum_is_never_installed(tmp_path):
    setup = (
        serve_zip(tmp_path, checksum="0" * 64)
        + "$global:Have = @('winget'); $global:WingetProvides = 'uv'\n"
    )
    r = run_block(tmp_path, "Install-Uv", setup=setup)
    assert r["result"] == "RESULT=OK", r["out"]  # winget took over
    assert not (r["bin"] / "uv.exe").exists(), "a zip with the wrong checksum was unpacked"
    assert "the checksum of uv-x86_64-pc-windows-msvc.zip does not match" in r["out"]
    winget = next(c for c in r["calls"] if c.startswith("winget"))
    assert "--id astral-sh.uv" in winget and " -e " in winget and "--source winget" in winget
    assert "--accept-package-agreements" in winget and "--accept-source-agreements" in winget


@windows_only
def test_a_checksum_file_that_is_not_a_checksum_is_refused(tmp_path):
    zpath, _ = make_zip(tmp_path)
    setup = web(
        {
            ZIP: f"Copy-Item -LiteralPath {q(zpath)} -Destination $f -Force",
            ZIP
            + ".sha256": "Set-Content -LiteralPath $f -Value '<html>blocked by your administrator</html>'",
        }
    )
    r = run_block(tmp_path, "Install-Uv", setup=setup)
    assert r["result"].startswith("RESULT=THROW"), r["out"]
    assert not (r["bin"] / "uv.exe").exists()
    assert "does not match" in r["out"]


@windows_only
def test_an_installer_that_exits_with_an_error_neither_ends_the_session_nor_the_fallbacks(tmp_path):
    """astral.sh answered, but the installer could not fetch the program itself (its second host is blocked). The real
    installers of uv and Claude Code end with `exit 1`: run by Invoke-Expression that `exit` closed the whole PowerShell window,
    so the fallbacks never ran and the error was lost. Here the session must live on to print RESULT."""
    failing = "Write-Output 'could not download uv.exe from releases.astral.sh'\nexit 1\n"
    r = run_block(
        tmp_path,
        "Install-Uv",
        setup=serve_zip(tmp_path).replace(
            "$global:Web = @{\n",
            "$global:Web = @{\n  " + q(ASTRAL) + " = { param($f) " + script_act(failing) + " }\n",
        ),
    )
    assert r["result"] == "RESULT=OK", r["out"]
    assert (
        "astral.sh installer: the installer ended with exit code 1 (could not download uv.exe from releases.astral.sh)"
        in r["out"]
    )
    assert (r["bin"] / "uv.exe").exists()  # the GitHub route took over


@windows_only
def test_a_route_that_ends_without_the_tool_on_the_path_does_not_count(tmp_path):
    setup = (
        web({ASTRAL: script_act("$null = 1\n")})
        + "$global:Have = @('winget'); $global:WingetProvides = 'uv'\n"
    )
    r = run_block(tmp_path, "Install-Uv", setup=setup)
    assert r["result"] == "RESULT=OK", r["out"]
    assert "astral.sh installer: finished, but 'uv' is still not on the PATH" in r["out"]
    assert any(c.startswith("winget") for c in r["calls"])


@windows_only
def test_when_every_route_fails_the_error_says_what_was_tried_and_which_hosts_answer(tmp_path):
    setup = web({"https://github.com": "", "https://pypi.org": ""})  # only these answer
    r = run_block(tmp_path, "Install-Uv", setup=setup)
    assert r["result"].startswith("RESULT=THROW: claude-hr-install failed: could not install uv"), (
        r["out"]
    )
    out = r["out"]
    assert (
        "astral.sh installer: https://astral.sh/uv/install.ps1 - Unable to connect to the remote server"
        in out
    )
    assert (
        "GitHub release: https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
        in out
    )
    assert "winget: winget is not installed" in out
    assert re.search(r"https://github\.com\s+reachable", out) and re.search(
        r"https://astral\.sh\s+NOT reachable", out
    )
    assert re.search(r"https://releases\.astral\.sh\s+NOT reachable", out)
    assert "Install uv another way (https://docs.astral.sh/uv/getting-started/installation/)" in out


@windows_only
def test_a_winget_that_fails_reports_its_exit_code(tmp_path):
    r = run_block(
        tmp_path, "Install-Uv", setup="$global:Have = @('winget'); $global:WingetExit = 1\n"
    )
    assert r["result"].startswith("RESULT=THROW"), r["out"]
    assert "winget: winget exited with code 1" in r["out"]


@windows_only
def test_claude_code_falls_back_to_winget_when_claude_ai_cannot_be_reached(tmp_path):
    r = run_block(
        tmp_path,
        "Install-ClaudeCode",
        setup="$global:Have = @('winget'); $global:WingetProvides = 'claude'\n",
    )
    assert r["result"] == "RESULT=OK", r["out"]
    assert f"Get {CLAUDE}" in r["calls"]
    assert any(c.startswith("winget") and "--id Anthropic.ClaudeCode" in c for c in r["calls"])


@windows_only
def test_claude_code_that_cannot_be_installed_says_where_to_get_it(tmp_path):
    r = run_block(tmp_path, "Install-ClaudeCode")
    assert r["result"].startswith(
        "RESULT=THROW: claude-hr-install failed: could not install claude"
    ), r["out"]
    assert "claude.ai installer: https://claude.ai/install.ps1 - Unable to connect" in r["out"]
    assert "https://code.claude.com/docs/en/setup" in r["out"]


# ---- "found" is not "works" --------------------------------------------------------------------------------------------
@windows_only
def test_a_tool_that_installs_but_will_not_run_is_not_a_success(tmp_path):
    """The second report: claude.ai blocked, winget 'installed' Claude Code, and Windows then refused to start claude.exe
    ("Program 'claude.exe' failed to run: Access is denied"). That is a failed install with the reason and the way to check it,
    not a crash at the last line of the script."""
    r = run_block(
        tmp_path,
        "Install-ClaudeCode",
        setup="$global:Have = @('winget'); $global:WingetProvides = 'claude'; $global:Broken = @('claude')\n",
    )
    assert r["result"].startswith(
        "RESULT=THROW: claude-hr-install failed: could not install claude"
    ), r["out"]
    out = r["out"]
    assert "claude.ai installer: https://claude.ai/install.ps1 - Unable to connect" in out
    assert (
        "winget: installed at C:\\fake\\claude.exe, but it will not run: Program 'claude.exe' failed to run: Access is denied"
        in out
    )
    assert "& 'C:\\fake\\claude.exe' --version" in out and "ask whoever manages this PC" in out


@windows_only
def test_an_install_that_will_not_run_hands_over_to_the_next_route(tmp_path):
    zpath, digest = make_zip(tmp_path)
    setup = web(
        {
            ASTRAL: script_act(fake_uv(tmp_path / "bin")),
            ZIP: f"Copy-Item -LiteralPath {q(zpath)} -Destination $f -Force; $global:Broken = @()",
            ZIP
            + ".sha256": f"Set-Content -LiteralPath $f -Value '{digest}  uv-x86_64-pc-windows-msvc.zip'",
        }
    )
    r = run_block(tmp_path, "Install-Uv", setup=setup + "$global:Broken = @('uv')\n")
    assert r["result"] == "RESULT=OK", r["out"]
    assert "astral.sh installer: installed at C:\\fake\\uv.exe, but it will not run" in r["out"]
    assert (
        r["bin"] / "uv.exe"
    ).read_bytes() == b"MZ stub of uv.exe"  # the zip's, not the first route's


def test_the_last_line_of_the_installer_cannot_crash_on_a_program_that_will_not_start():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "N { claude --version }" not in text, (
        "an unguarded call is what crashed with 'Access is denied'"
    )
    assert re.search(r"^Show-ClaudeCode$", text, re.M), (
        "the script ends its Claude Code check through the guarded function"
    )


@windows_only
def test_a_claude_code_that_was_there_before_and_will_not_run_is_only_a_warning(tmp_path):
    """The last step of the installer: not a crash, claude-hr is installed anyway."""
    r = run_block(tmp_path, "Show-ClaudeCode; 'AFTER'", setup="$global:Broken = @('claude')\n")
    assert r["result"] == "RESULT=OK" and "AFTER" in r["out"], r["out"]
    assert (
        "WARNING: Claude Code (C:\\fake\\claude.exe) did not run: Program 'claude.exe' failed to run: Access is denied"
        in r["out"]
    )


@windows_only
def test_a_claude_code_that_runs_is_logged_with_its_version(tmp_path):
    r = run_block(tmp_path, "Show-ClaudeCode")
    assert r["result"] == "RESULT=OK" and "[claude-hr-install] claude 9.9.9" in r["out"], r["out"]


@windows_only
def test_the_hint_survives_a_path_with_an_apostrophe(tmp_path):
    """The command in the hint is meant to be pasted: `C:\\Users\\O'Brien\\...` needs its quote doubled."""
    r = run_block(
        tmp_path,
        "Install-ClaudeCode",
        setup="$global:Have = @('winget'); $global:WingetProvides = 'claude'; $global:Broken = @('claude')\n"
        '$global:FakeDir = "C:\\Users\\O\'Brien\\bin"\n',
    )
    assert "& 'C:\\Users\\O''Brien\\bin\\claude.exe' --version" in r["out"], r["out"]


def test_claude_hr_ctl_is_started_once_before_setup_runs_it():
    """The third report: ThreatLocker removed, Claude Code ran, then `claude-hr-ctl.exe` (a launcher uv had just written) was
    refused and `N { claude-hr-ctl @setupArgs }` crashed with a raw exception. The check sits between the install and setup."""
    text = INSTALLER.read_text(encoding="utf-8")
    install = text.index("N { uv tool install")
    check = text.index("\nAssert-HrCtlRuns\n")
    setup = text.index("N { claude-hr-ctl @setupArgs }")
    assert install < check < setup


@windows_only
def test_claude_hr_ctl_that_starts_lets_the_install_go_on(tmp_path):
    r = run_block(tmp_path, "Assert-HrCtlRuns; 'AFTER'")
    assert r["result"] == "RESULT=OK" and "AFTER" in r["out"], r["out"]


@windows_only
def test_claude_hr_ctl_that_will_not_start_stops_the_install_with_the_reason_and_the_way_out(
    tmp_path,
):
    r = run_block(tmp_path, "Assert-HrCtlRuns", setup="$global:Broken = @('claude-hr-ctl')\n")
    out = r["out"]
    assert (
        r["result"]
        == "RESULT=THROW: claude-hr-install failed: claude-hr-ctl will not start (details above)"
    ), out
    assert (
        "claude-hr-ctl was installed at C:\\fake\\claude-hr-ctl.exe but will not start: "
        "Program 'claude-hr-ctl.exe' failed to run: Access is denied"
    ) in out
    assert (
        "& 'C:\\fake\\claude-hr-ctl.exe' --version" in out and "ask whoever manages this PC" in out
    )
    assert "ThreatLocker" in out and "restart Windows" in out
    assert "a rule has to allow both folders" in out and "%APPDATA%\\uv\\tools" in out


@windows_only
def test_a_real_claude_hr_ctl_that_windows_refuses_ends_in_that_message_not_in_an_exception(
    tmp_path,
):
    """Your situation with a real program: claude-hr-ctl.exe is found on the PATH and Windows answers 'Access is denied'."""
    exe = _program(tmp_path / "tools", "claude-hr-ctl.exe")
    user = _deny_execute(exe)
    try:
        r = run_block(
            tmp_path,
            "Assert-HrCtlRuns",
            mocks=False,
            pre=_on_path(exe.parent),
            setup=COUNT_SLEEPS,
        )
    finally:
        subprocess.run(["icacls", str(exe), "/remove:d", user], capture_output=True)
    out = r["out"]
    assert r["result"].startswith(
        "RESULT=THROW: claude-hr-install failed: claude-hr-ctl will not start"
    ), out
    assert "claude-hr-ctl was installed at " in out and "failed to run: Access is denied" in out
    assert "At line" not in out


# ---- a failing git or uv says why ------------------------------------------------------------------------------------------
def test_a_failing_step_shows_what_the_program_printed_instead_of_guessing():
    """The fourth report: `git fetch` failed in an Administrator window and the installer answered '(check the URL and
    credentials)' because it had thrown git's output away. git's own message is the only thing that names the cause."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "(check the URL and credentials)" not in text
    for call in ("git -C $src fetch", "git clone --quiet", "uv tool install --python 3.13"):
        line = next(ln for ln in text.splitlines() if call in ln and "N {" in ln)
        assert "Out-Null" not in line, f"{call}: its output is thrown away again"
    assert "Stop-WithOutput 'git could not fetch the claude-hr repository' $gitOut" in text
    assert "Stop-WithOutput 'uv tool install failed' $uvOut" in text


@windows_only
def test_the_last_lines_a_program_printed_are_shown_and_the_hint_follows(tmp_path):
    lines = "; ".join(f"'line {i}'" for i in range(1, 11))
    r = run_block(
        tmp_path,
        f"Stop-WithOutput 'the step failed' @({lines}) 'Try this instead.'",
        mocks=False,
    )
    out = r["out"]
    assert (
        r["result"] == "RESULT=THROW: claude-hr-install failed: the step failed (details above)"
    ), out
    assert "ERROR: the step failed" in out and "it printed:" in out
    assert "line 4" not in out and all(f"line {i}" in out for i in range(5, 11)), (
        "the last six lines, no more"
    )
    assert out.index("line 10") < out.index("Try this instead.")


@windows_only
def test_a_step_that_printed_nothing_says_so(tmp_path):
    r = run_block(tmp_path, "Stop-WithOutput 'the step failed' @()", mocks=False)
    assert "it printed nothing" in r["out"], r["out"]


@windows_only
def test_gits_own_message_reaches_the_screen_when_a_fetch_fails(tmp_path):
    """Real git, a folder that is not a repository: what the old installer hid behind its guess."""
    if shutil.which("git") is None:
        pytest.skip("needs git")
    folder = tmp_path / "not-a-repo"
    folder.mkdir()
    scenario = (
        f"$o = @(N {{ git -C {q(folder)} fetch --quiet --tags --prune origin }}); "
        "Stop-WithOutput 'git could not fetch the claude-hr repository' $o 'hint'"
    )
    r = run_block(
        tmp_path,
        scenario,
        mocks=False,
        pre="$env:GIT_CEILING_DIRECTORIES = " + q(tmp_path.parent) + "\n",
    )
    assert "fatal: not a git repository" in r["out"], r["out"]


@windows_only
def test_the_dubious_ownership_message_of_git_is_shown_with_the_way_out(tmp_path):
    """What running the installer in an Administrator window can give: git refuses a folder another token created."""
    fake_git = (
        "function git { "
        "'fatal: detected dubious ownership in repository at ''C:/Users/x/.claude-hr/repo'''; "
        "'To add an exception for this directory, call:'; ''; "
        "'    git config --global --add safe.directory C:/Users/x/.claude-hr/repo'; "
        "$global:LASTEXITCODE = 128 }\n"
    )
    scenario = (
        "$o = @(N { git -C x fetch }); "
        "Stop-WithOutput 'git could not fetch the claude-hr repository' $o "
        "('If git says \"dubious ownership\", run this installer in a normal (not Administrator) PowerShell window.')"
    )
    r = run_block(tmp_path, scenario, mocks=False, setup=fake_git)
    out = r["out"]
    assert "fatal: detected dubious ownership in repository at" in out
    assert "git config --global --add safe.directory C:/Users/x/.claude-hr/repo" in out
    assert "normal (not Administrator) PowerShell window" in out


def _ps_text(value: str) -> str:
    """A PowerShell expression that yields exactly `value` (newlines and quotes included)."""
    b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))"


@windows_only
@pytest.mark.parametrize(
    ("message", "reason"),
    [
        # the reported one: PowerShell glues its trace straight onto the text
        (
            "Program 'claude.exe' failed to run: Access is deniedAt line:201 char:24\r\n+ Log (...)\r\n+   ~~~~",
            "Program 'claude.exe' failed to run: Access is denied",
        ),
        ("failed to start\nAt C:\\x\\y.ps1:3 char:1\n+ foo", "failed to start"),
        ("failed to start\r\nAt line:3 char:1\r\n+ foo", "failed to start"),
        # sentences that merely mention a place are left alone
        (
            "Could not read the file at C:\\data\\x.txt",
            "Could not read the file at C:\\data\\x.txt",
        ),
        ("Problem. At C:\\data nothing was found", "Problem. At C:\\data nothing was found"),
        ("Look at line:5 of the file", "Look at line:5 of the file"),
        ("  padded  ", "padded"),
        ("", ""),
    ],
)
def test_the_reason_of_an_error_loses_powershells_trace_and_nothing_else(tmp_path, message, reason):
    r = run_block(
        tmp_path, f"'REASON=[' + (Get-ErrorReason ({_ps_text(message)})) + ']'", mocks=False
    )
    assert f"REASON=[{reason}]" in r["out"], r["out"]


CURL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"


def _program(folder: Path, name: str) -> Path:
    """A real program that answers --version: a copy of curl.exe (it needs nothing next to it)."""
    if not CURL.exists():
        pytest.skip("needs curl.exe (Windows 10 and later)")
    folder.mkdir(exist_ok=True)
    target = folder / name
    shutil.copy2(CURL, target)
    return target


def _deny_execute(path: Path) -> str:
    """Take the right to run `path` from the current user: Windows then answers 'Access is denied', as on the reported PC."""
    user = os.environ.get("USERNAME", "")
    subprocess.run(["icacls", str(path), "/deny", f"{user}:(X)"], capture_output=True, check=True)
    try:
        subprocess.run([str(path), "--version"], capture_output=True)
    except PermissionError:
        return user
    subprocess.run(["icacls", str(path), "/remove:d", user], capture_output=True)
    pytest.skip("this machine still starts a program whose execute right is denied")


def _on_path(folder: Path) -> str:
    return f"$env:PATH = {q(folder)} + ';' + $env:PATH\n"


COUNT_SLEEPS = "$global:Sleeps = 0; function Start-Sleep { $global:Sleeps++ }\n"


def _report(name: str) -> str:
    return (
        f"$r = Test-ToolRuns {q(name)}; 'OK=' + $r.Ok; 'VERSION=' + $r.Version; 'ERROR=' + $r.Error; "
        "'PATH=' + $r.Path; 'SLEEPS=' + $global:Sleeps"
    )


def _line(out: str, key: str) -> str:
    return next(ln for ln in out.splitlines() if ln.startswith(key + "="))[len(key) + 1 :]


@windows_only
def test_a_tool_that_runs_is_reported_with_its_version_and_path(tmp_path):
    exe = _program(tmp_path / "tools", "hrtool.exe")
    r = run_block(
        tmp_path, _report("hrtool"), mocks=False, pre=_on_path(exe.parent), setup=COUNT_SLEEPS
    )
    assert _line(r["out"], "OK") == "True", r["out"]
    assert _line(r["out"], "VERSION").startswith("curl "), r["out"]
    assert _line(r["out"], "PATH").lower() == str(exe).lower()
    assert _line(r["out"], "SLEEPS") == "0"


@windows_only
def test_a_tool_that_starts_but_fails_is_reported_at_once_without_waiting(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "hrfail.cmd").write_text("@exit /b 7\r\n", encoding="ascii")
    r = run_block(
        tmp_path,
        _report("hrfail"),
        mocks=False,
        pre=_on_path(tmp_path / "tools"),
        setup=COUNT_SLEEPS,
    )
    assert _line(r["out"], "OK") == "False" and "exited with code 7" in _line(r["out"], "ERROR"), r[
        "out"
    ]
    assert _line(r["out"], "SLEEPS") == "0", (
        "a program that started is not a scanner delay: no waiting"
    )


@windows_only
def test_a_tool_that_is_not_on_the_path_says_so(tmp_path):
    r = run_block(tmp_path, _report("hr-no-such-tool"), mocks=False, setup=COUNT_SLEEPS)
    assert _line(r["out"], "OK") == "False" and "is not on the PATH" in _line(r["out"], "ERROR"), r[
        "out"
    ]


@windows_only
def test_windows_refusing_to_start_a_program_is_a_result_not_a_crash(tmp_path):
    """The reported error, for real: 'Program ... failed to run: Access is denied'. Tried four times, then reported cleanly."""
    exe = _program(tmp_path / "tools", "hrdenied.exe")
    user = _deny_execute(exe)
    try:
        r = run_block(
            tmp_path, _report("hrdenied"), mocks=False, pre=_on_path(exe.parent), setup=COUNT_SLEEPS
        )
    finally:
        subprocess.run(["icacls", str(exe), "/remove:d", user], capture_output=True)
    assert _line(r["out"], "OK") == "False", r["out"]
    error = _line(r["out"], "ERROR")
    assert "failed to run: Access is denied" in error, error
    assert "At line" not in error and "At C:" not in error, (
        "PowerShell's own trace is not part of the reason"
    )
    assert _line(r["out"], "SLEEPS") == "3"  # four attempts, a pause between each
    assert _line(r["out"], "PATH").lower() == str(exe).lower()


@windows_only
def test_a_program_that_a_virus_scan_still_holds_is_tried_again(tmp_path):
    """Denied on the first try, allowed on the second: what a scanner finishing with a just-written file looks like."""
    exe = _program(tmp_path / "tools", "hrscan.exe")
    user = _deny_execute(exe)
    allow = f"function Start-Sleep {{ $global:Sleeps++; & icacls {q(exe)} /remove:d {q(user)} | Out-Null }}\n"
    try:
        r = run_block(
            tmp_path,
            _report("hrscan"),
            mocks=False,
            pre=_on_path(exe.parent),
            setup="$global:Sleeps = 0\n" + allow,
        )
    finally:
        subprocess.run(["icacls", str(exe), "/remove:d", user], capture_output=True)
    assert _line(r["out"], "OK") == "True", r["out"]
    assert _line(r["out"], "SLEEPS") == "1"


@windows_only
def test_a_blocked_program_found_by_a_route_ends_in_the_clear_message_not_a_stack_trace(tmp_path):
    """The whole path of the report with the real checks: the route 'succeeds', the program is found, Windows refuses it."""
    exe = _program(tmp_path / "tools", "hrblocked.exe")
    user = _deny_execute(exe)
    try:
        r = run_block(
            tmp_path,
            "Install-Tool 'hrblocked' @(@{ Name = 'winget'; Run = { } }) 'Install it another way, then run this installer again.'",
            mocks=False,
            pre=_on_path(exe.parent),
            setup=COUNT_SLEEPS + "function Show-Reachability { }\n",
        )
    finally:
        subprocess.run(["icacls", str(exe), "/remove:d", user], capture_output=True)
    out = r["out"]
    assert r["result"].startswith(
        "RESULT=THROW: claude-hr-install failed: could not install hrblocked"
    ), out
    assert "winget: installed at " in out and exe.name in out
    assert "but it will not run: Program 'hrblocked.exe' failed to run: Access is denied" in out
    assert "--version" in out and "ask whoever manages this PC" in out


@windows_only
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (0, 0),  # .NET 4.7+: the system decides (TLS 1.3 included): left alone
        (192, 192 | 3072),  # TLS 1.0 + 1.1 only (older .NET): TLS 1.2 is added
        (3072, 3072),  # already has TLS 1.2
    ],
)
def test_tls_12_is_added_only_where_the_default_lacks_it(tmp_path, before, after):
    r = run_block(
        tmp_path,
        "'TLS=' + [int][Net.ServicePointManager]::SecurityProtocol",
        pre=f"[Net.ServicePointManager]::SecurityProtocol = {before}\n",
    )
    assert f"TLS={after}" in r["out"], r["out"]


# ---- the helpers against a real (local) server ------------------------------------------------------------------------
@pytest.fixture
def local_404_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(404)
            self.end_headers()

        do_GET = do_HEAD

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@windows_only
def test_an_error_page_counts_as_reachable_and_a_refused_connection_does_not(
    tmp_path, local_404_server
):
    scenario = (
        "[Net.WebRequest]::DefaultWebProxy = $null; "
        f"'ERRORPAGE=' + (Test-Reachable 'http://127.0.0.1:{local_404_server}/x'); "
        f"'REFUSED=' + (Test-Reachable 'http://127.0.0.1:{_closed_port()}/x')"
    )
    r = run_block(tmp_path, scenario, mocks=False)
    assert "ERRORPAGE=True" in r["out"] and "REFUSED=False" in r["out"], r["out"]


@windows_only
def test_a_download_error_names_the_url_and_a_refused_connection_is_retried_once(tmp_path):
    url = f"http://127.0.0.1:{_closed_port()}/uv.zip"
    scenario = (
        f"[Net.WebRequest]::DefaultWebProxy = $null; Get-Download {q(url)} {q(tmp_path / 'x.bin')}"
    )
    r = run_block(tmp_path, scenario, mocks=False)
    assert r["result"].startswith(f"RESULT=THROW: {url} - "), r["out"]
    assert not (tmp_path / "x.bin").exists() or (tmp_path / "x.bin").stat().st_size == 0


@windows_only
def test_the_session_path_keeps_its_order_and_gets_no_duplicates(tmp_path):
    scenario = "$before = $env:PATH; Update-SessionPath; Update-SessionPath; " + (
        "$parts = $env:PATH -split ';'; "
        "'FIRST=' + $parts[0]; "
        "'DUPES=' + @($parts | Where-Object { $_ } | Group-Object { $_.TrimEnd('\\').ToLower() } | Where-Object Count -gt 1).Count; "
        # an entry counts as kept when its folder is still there, however the case or a trailing backslash is spelled: the
        # function merges those on purpose (a runner's PATH has both `C:\\x` and `C:\\x\\`)
        "$kept = @($parts | ForEach-Object { $_.TrimEnd('\\').ToLower() }); "
        "'LOST=' + (@(($before -split ';') | Where-Object { $_ -and ($kept -notcontains $_.TrimEnd('\\').ToLower()) }) -join ' | ')"
    )
    r = run_block(tmp_path, scenario)
    assert f"FIRST={r['bin']}" in r["out"] and "DUPES=0" in r["out"], r["out"]
    assert "LOST=\n" in r["out"] + "\n", r["out"]  # nothing of the original PATH is gone
