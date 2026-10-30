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
