"""Credential hygiene: the literal-secret scan of Claude's configuration (project-scoped MCP servers included) and the
transcript scan. No test may print, log or store a secret value: every one asserts the raw values stay out."""

import dataclasses
import json
import logging
import os
import random
import stat
import string
import threading
import time
from pathlib import Path

import pytest

from claude_hr import config, creds, ctl, paths
from claude_hr.checks import INFO, OK, WARN


def fake(
    prefix: str, n: int, alphabet: str = string.ascii_letters + string.digits, seed: int = 1
) -> str:
    """A random-looking value built at run time, so no token-shaped string sits in this file's source."""
    rnd = random.Random(f"{prefix}{seed}")
    return prefix + "".join(rnd.choice(alphabet) for _ in range(n))


UPPER = string.ascii_uppercase + string.digits
AWS_TEMP_ID = fake("ASIA", 16, UPPER)  # an STS access key id
AWS_LONG_ID = fake("AKIA", 16, UPPER)  # a long-lived access key id
AWS_SECRET = fake("", 40, string.ascii_letters + string.digits + "/+")
AWS_SESSION = fake("IQoJ", 300, string.ascii_letters + string.digits + "/+=")
BEARER = fake("", 32)
GH_TOKEN = fake("ghp_", 36)
GH_TOKEN_2 = fake("ghs_", 36, seed=2)
DB_PW = fake("Pw", 14, seed=3)  # a password inside a connection string
OPAQUE = fake("", 24, seed=4)  # an opaque bearer token (no known shape)
RAW = [
    AWS_TEMP_ID,
    AWS_LONG_ID,
    AWS_SECRET,
    AWS_SESSION,
    BEARER,
    GH_TOKEN,
    GH_TOKEN_2,
    DB_PW,
    OPAQUE,
]
assert not any(
    w in v.lower() for v in RAW for w in creds._PLACEHOLDER_WORDS
)  # a placeholder would not be flagged


def no_raw(*texts: str) -> None:
    """Neither a value nor even five characters of it (the mask shows at most four) is in any text."""
    for text in texts:
        for v in RAW:
            assert v not in text and v[:5] not in text, "a secret value reached the output"


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    """`gh` and `op` on the machine running the suite must not change what run_checks reports."""
    monkeypatch.setattr(creds.procutil, "cmd_exists", lambda name: False)


def claude_json(data) -> None:
    paths.claude_global_json().write_text(json.dumps(data), encoding="utf-8")


def aws_env(**extra):
    return {
        "AWS_ACCESS_KEY_ID": AWS_TEMP_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET,
        "AWS_SESSION_TOKEN": AWS_SESSION,
        "AWS_REGION": "ap-southeast-2",
        **extra,
    }


def project_with(servers) -> dict:
    return {"mcpServers": {}, "projects": {"C:/work/HalfCourt - AWS": {"mcpServers": servers}}}


def check(name: str):
    return next(c for c in creds.run_checks(config.load()) if c.name == name)


# ---- project-scoped MCP servers ---------------------------------------------------------------------------------


def test_temporary_aws_credentials_in_a_project_scoped_server_are_found_and_masked():
    """doctor said 'MCP literal secrets: none' while ~/.claude.json held these under projects[<path>]."""
    claude_json(
        project_with({"aws-api": {"command": "uvx", "args": ["aws-api-mcp"], "env": aws_env()}})
    )
    found = creds.scan_config_secrets()
    assert [(f.project, f.server, f.field, f.name, f.kind) for f in found] == [
        ("C:/work/HalfCourt - AWS", "aws-api", "env", "AWS_ACCESS_KEY_ID", "AWS access key id"),
        (
            "C:/work/HalfCourt - AWS",
            "aws-api",
            "env",
            "AWS_SECRET_ACCESS_KEY",
            "AWS secret access key",
        ),
        ("C:/work/HalfCourt - AWS", "aws-api", "env", "AWS_SESSION_TOKEN", "AWS session token"),
    ]  # AWS_REGION is not a secret
    assert all(f.temporary for f in found)
    for f, raw in zip(found, (AWS_TEMP_ID, AWS_SECRET, AWS_SESSION), strict=True):
        assert f.masked.split("…")[0] == raw[:4]  # the first four characters at most
        assert f"({len(raw)} chars)" in f.masked
        assert "probably expired" in f.fix and "delete" in f.fix
    no_raw(repr(found), *(f.fix + f.where for f in found))


def test_top_level_servers_are_still_scanned_and_name_their_kind():
    claude_json(
        {
            "mcpServers": {
                "gh": {"env": {"GITHUB_TOKEN": GH_TOKEN}},
                "api": {"headers": {"Authorization": f"Bearer {BEARER}", "X-Trace": "on"}},
            }
        }
    )
    found = creds.scan_config_secrets()
    assert [(f.project, f.server, f.field, f.name, f.kind) for f in found] == [
        ("", "gh", "env", "GITHUB_TOKEN", "GitHub token"),
        ("", "api", "headers", "Authorization", "bearer token"),
    ]
    assert found[0].where == ".claude.json [user] gh env.GITHUB_TOKEN"
    assert not any(f.temporary for f in found)
    assert "rotate" in found[0].fix and "expired" not in found[0].fix
    assert found[1].masked.startswith(BEARER[:4]) and "Bear" not in found[1].masked
    no_raw(repr(found))


def test_references_placeholders_and_non_secrets_are_not_flagged():
    env = {
        "API_TOKEN_1": "${GITHUB_TOKEN}",
        "API_TOKEN_2": "${GITHUB_TOKEN:-}",
        "API_TOKEN_3": "$GITHUB_TOKEN",
        "API_TOKEN_4": "%GITHUB_TOKEN%",
        "API_TOKEN_5": "op://Private/GitHub/token",
        "API_TOKEN_6": "{{ secrets.token }}",
        "API_TOKEN_7": "<paste-your-token>",
        "API_TOKEN_8": "your-api-key-goes-here",
        "API_TOKEN_9": "changeme-please",
        "MAX_TOKENS": "4096",
        "TOKEN_DEBUG": "true",
        "EMPTY_SECRET": "",
        "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
        "AWS_REGION": "ap-southeast-2",
    }
    server = {
        "env": env,
        "headers": {"Authorization": "Bearer ${TOKEN}", "X-Api-Key": "$KEY"},
        "args": [
            "--token=${TOKEN}",
            "--api-key",
            "${KEY}",
            "-e",
            "AWS_SECRET_ACCESS_KEY",
            "--password=op://Private/db/password",
            "--verbose",
        ],
        "url": "https://mcp.example.test/sse?api_key=${KEY}",
    }
    claude_json(
        {"mcpServers": {"clean": server}, "projects": {"C:/p": {"mcpServers": {"clean": server}}}}
    )
    assert creds.scan_config_secrets() == []
    c = check("MCP literal secrets")
    assert c.status == OK and c.detail == "none"


def test_args_and_urls_are_scanned_too():
    claude_json(
        {
            "projects": {
                "C:/p": {
                    "mcpServers": {
                        "s": {
                            "command": "npx",
                            "args": [
                                f"--api-key={BEARER}",
                                "--password",
                                BEARER,  # the value of the flag before it: reported once, with the flag
                                "-e",
                                f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}",
                                GH_TOKEN_2,
                                "--region=ap-southeast-2",
                            ],
                            "url": f"https://user:{BEARER}@mcp.example.test/sse",
                        },
                        "q": {"url": f"https://mcp.example.test/sse?api_key={BEARER}&mode=fast"},
                    }
                }
            }
        }
    )
    found = creds.scan_config_secrets()
    assert [(f.server, f.field, f.name, f.kind) for f in found] == [
        ("s", "args", "api-key", "API key"),
        ("s", "args", "password", "password"),
        ("s", "args", "AWS_SECRET_ACCESS_KEY", "AWS secret access key"),
        ("s", "args", "[5]", "GitHub token"),
        ("s", "url", "password", "password"),
        ("q", "url", "?api_key", "API key"),
    ]
    no_raw(repr(found))


# ---- settings files and project files ---------------------------------------------------------------------------


def test_settings_env_and_mcp_servers_are_scanned_at_user_level():
    (paths.claude_home() / "settings.json").write_text(
        json.dumps({"env": {"DB_PASSWORD": BEARER, "DISABLE_ERROR_REPORTING": "1"}}),
        encoding="utf-8",
    )
    (paths.claude_home() / "settings.local.json").write_text(
        json.dumps(
            {
                "env": {
                    "SOME_NEUTRAL_NAME": GH_TOKEN
                },  # no secret-looking name, but a token's shape
                "mcpServers": {"s": {"env": {"AUTH_TOKEN": BEARER}}},
            }
        ),
        encoding="utf-8",
    )
    found = creds.scan_config_secrets()
    assert [(f.file, f.server, f.field, f.name) for f in found] == [
        ("settings.json", "", "env", "DB_PASSWORD"),
        ("settings.local.json", "", "env", "SOME_NEUTRAL_NAME"),
        ("settings.local.json", "s", "env", "AUTH_TOKEN"),
    ]
    env_check, mcp_check = check("settings.json env secrets"), check("MCP literal secrets")
    assert env_check.status == WARN
    assert (
        env_check.detail
        == "secret-looking variable(s) stored in plaintext: DB_PASSWORD, SOME_NEUTRAL_NAME"
    )
    assert mcp_check.status == WARN and "1 literal secret(s) found" in mcp_check.detail
    no_raw(repr(found), env_check.line(), mcp_check.line())


def test_a_projects_own_files_are_scanned(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"db": {"env": {"DB_PASSWORD": BEARER}}}}), encoding="utf-8"
    )
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"GH_TOKEN": GH_TOKEN}}), encoding="utf-8"
    )
    (proj / ".claude" / "settings.local.json").write_text(
        json.dumps({"mcpServers": {"s": {"headers": {"X-Api-Key": BEARER}}}}), encoding="utf-8"
    )
    claude_json({"projects": {str(proj): {}}})
    found = creds.scan_config_secrets()
    assert [(f.file, f.project, f.server, f.name) for f in found] == [
        (".mcp.json", str(proj), "db", "DB_PASSWORD"),
        ("settings.json", str(proj), "", "GH_TOKEN"),
        ("settings.local.json", str(proj), "s", "X-Api-Key"),
    ]
    env_check = check("settings.json env secrets")
    assert "GH_TOKEN (project " in env_check.detail
    no_raw(repr(found), env_check.line())


# ---- temporary AWS credentials ----------------------------------------------------------------------------------


def test_aws_values_beside_a_session_token_or_an_asia_key_are_temporary_and_others_are_not():
    claude_json(
        {
            "mcpServers": {
                "sts": {
                    "env": {"AWS_ACCESS_KEY_ID": AWS_LONG_ID, "AWS_SESSION_TOKEN": AWS_SESSION}
                },
                "key-only": {
                    "env": {"AWS_ACCESS_KEY_ID": AWS_TEMP_ID, "AWS_SECRET_ACCESS_KEY": AWS_SECRET}
                },
                "long": {
                    "env": {"AWS_ACCESS_KEY_ID": AWS_LONG_ID, "AWS_SECRET_ACCESS_KEY": AWS_SECRET}
                },
            }
        }
    )
    got = {(f.server, f.name): f.temporary for f in creds.scan_config_secrets()}
    assert got == {
        (
            "sts",
            "AWS_ACCESS_KEY_ID",
        ): True,  # next to a session token, so it is an STS key whatever its prefix
        ("sts", "AWS_SESSION_TOKEN"): True,
        ("key-only", "AWS_ACCESS_KEY_ID"): True,  # ASIA
        ("key-only", "AWS_SECRET_ACCESS_KEY"): True,
        ("long", "AWS_ACCESS_KEY_ID"): False,
        ("long", "AWS_SECRET_ACCESS_KEY"): False,
    }
    long_lived = [f for f in creds.scan_config_secrets() if f.server == "long"]
    assert all("expired" not in f.fix and "profile" in f.fix for f in long_lived)


# ---- doctor's line ----------------------------------------------------------------------------------------------


def test_doctors_line_counts_the_secrets_and_names_the_first_two_places():
    claude_json(project_with({"aws-api": {"env": aws_env()}}))
    c = check("MCP literal secrets")
    wheres = [f.where for f in creds.scan_config_secrets()]
    assert c.status == WARN
    assert c.detail == f"3 literal secret(s) found: {wheres[0]}; {wheres[1]}; +1 more"
    assert "HalfCourt - AWS" in c.detail and "aws-api" in c.detail
    assert (
        "AWS_ACCESS_KEY_ID" in c.detail and "AWS_SESSION_TOKEN" not in c.detail
    )  # only the first two
    assert "creds doctor" in c.fix and "probably expired" in c.fix
    no_raw(c.line(), json.dumps(dataclasses.asdict(c)))


def test_doctors_line_is_ok_when_there_is_nothing():
    assert (check("MCP literal secrets").status, check("MCP literal secrets").detail) == (
        OK,
        "none",
    )
    claude_json({"projects": {"C:/p": {"mcpServers": {"s": {"env": {"LOG_LEVEL": "debug"}}}}}})
    assert check("MCP literal secrets").detail == "none"


def test_creds_doctor_prints_each_finding_masked_with_its_fix(capsys, caplog):
    claude_json(project_with({"aws-api": {"env": aws_env()}}))
    with caplog.at_level(logging.DEBUG):
        rc = ctl._run(["creds", "doctor"])
    captured = capsys.readouterr()
    text = captured.out
    assert rc == 0  # a warning is not a failure
    assert "3 literal secret(s) in Claude's configuration (values are never printed):" in text
    assert "1. .claude.json [project C:/work/HalfCourt - AWS] aws-api env.AWS_ACCESS_KEY_ID" in text
    assert "project: C:/work/HalfCourt - AWS" in text
    assert "kind: AWS session token (temporary)" in text and "value: IQoJ…(304 chars)" in text
    assert "probably expired" in text and "delete" in text
    no_raw(text, captured.err, caplog.text)


# ---- never fail, never stall ------------------------------------------------------------------------------------


def test_a_broken_or_odd_config_file_finds_nothing_and_does_not_raise():
    paths.claude_global_json().write_text("{not json", encoding="utf-8")
    assert creds.scan_config_secrets() == []
    assert check("MCP literal secrets").status == OK
    claude_json(
        {
            "mcpServers": ["not", "a", "dict"],
            "projects": {
                "p": "a string",
                "q": {
                    "mcpServers": {
                        "s": "a string",
                        "t": {"env": "x", "args": "y", "url": 5, "headers": [1]},
                    }
                },
                7: {},
            },
        }
    )
    assert creds.scan_config_secrets() == []
    claude_json([1, 2, 3])
    assert creds.scan_config_secrets() == []


def test_a_huge_claude_json_is_not_read_and_doctor_says_so(monkeypatch):
    claude_json(project_with({"aws-api": {"env": aws_env()}}))
    assert len(creds.scan_config_secrets()) == 3
    monkeypatch.setattr(creds, "MAX_CONFIG_BYTES", 100)
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == []
    assert skipped == [".claude.json is larger than 100 bytes"]
    c = check("credentials")
    assert c.status == WARN and ".claude.json is larger than 100 bytes" in c.detail


def test_an_unparsable_claude_json_is_reported_as_not_scanned_not_as_clean():
    """A scan that could not read the file must not look like a scan that found nothing."""
    paths.claude_global_json().write_text(
        '{"mcpServers": {"s": {"env": {"API_TOKEN": "', encoding="utf-8"
    )
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == []
    assert skipped == [".claude.json is not valid JSON"]
    c = check("credentials")
    assert (
        c.status == WARN
        and c.detail == "the credential scan could not read: .claude.json is not valid JSON"
    )


def test_a_missing_file_and_a_projects_unparsable_file_are_not_reported(tmp_path):
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == [] and skipped == []  # no files at all
    assert not any(c.name == "credentials" for c in creds.run_checks(config.load()))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mcp.json").write_text(
        "{ // a comment: not JSON", encoding="utf-8"
    )  # a repo's own file
    claude_json({"projects": {str(proj): {}}})
    assert creds.scan_config_secrets(skipped) == [] and skipped == []
    assert not any(c.name == "credentials" for c in creds.run_checks(config.load()))


def test_only_the_most_recent_project_folders_are_read_and_findings_are_capped(
    tmp_path, monkeypatch
):
    projs = []
    for i in range(5):
        p = tmp_path / f"proj{i}"
        p.mkdir()
        (p / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"s": {"env": {"API_TOKEN": BEARER}}}}), encoding="utf-8"
        )
        projs.append(p)
    claude_json({"projects": {str(p): {} for p in projs}})
    assert len(creds.scan_config_secrets()) == 5
    monkeypatch.setattr(creds, "MAX_PROJECT_DIRS", 2)
    assert [f.project for f in creds.scan_config_secrets()] == [str(projs[3]), str(projs[4])]
    monkeypatch.setattr(creds, "MAX_PROJECT_DIRS", 60)
    monkeypatch.setattr(creds, "MAX_FINDINGS", 3)
    assert len(creds.scan_config_secrets()) == 3


def test_the_scan_stops_reading_project_folders_when_its_time_is_spent(tmp_path, monkeypatch):
    p = tmp_path / "proj"
    p.mkdir()
    (p / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"disk": {"env": {"API_TOKEN": BEARER}}}}), encoding="utf-8"
    )
    claude_json({"projects": {str(p): {"mcpServers": {"memory": {"env": {"API_TOKEN": BEARER}}}}}})
    assert [f.server for f in creds.scan_config_secrets()] == ["memory", "disk"]
    monkeypatch.setattr(creds, "SCAN_SECONDS", -1)
    assert [f.server for f in creds.scan_config_secrets()] == ["memory"]


def test_text_from_the_config_cannot_inject_terminal_escapes():
    evil = "evil\x1b[31m\nname"
    claude_json({"projects": {evil: {"mcpServers": {evil: {"env": {evil + "_TOKEN": BEARER}}}}}})
    (found,) = creds.scan_config_secrets()
    assert "\x1b" not in found.where and "\n" not in found.where and "\x1b" not in found.project
    assert "\x1b" not in "\n".join(creds.format_findings([found]))


def test_mask_never_shows_more_than_a_quarter_or_four_characters():
    assert creds.mask("ABCDEFGHIJ") == "AB…(10 chars)"
    assert creds.mask("abc") == "…(3 chars)"
    assert creds.mask("abcdefghijklmnop" * 4).startswith("abcd…")


# ---- the transcript scan ----------------------------------------------------------------------------------------


def transcript(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "user", "message": {"content": text}}) + "\n", encoding="utf-8"
    )
    return path


def test_the_transcript_scan_reads_subagent_and_workflow_transcripts_too(capsys):
    root = paths.claude_home() / "projects" / "proj"
    transcript(root / "s.jsonl", f"token {GH_TOKEN}")
    transcript(root / "s" / "subagents" / "agent-1.jsonl", f"key {AWS_LONG_ID}")
    transcript(
        root / "s" / "subagents" / "workflows" / "wf_1" / "agent-2.jsonl", f"again {GH_TOKEN_2}"
    )
    old = transcript(root / "old" / "subagents" / "agent-3.jsonl", f"stale {GH_TOKEN}")
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    res = creds.scan_transcripts(7)
    hits = res.hits
    assert (res.scanned, res.total, res.complete) == (3, 3, True)
    assert sorted((Path(f).name, kind) for f, kind, n in hits) == [
        ("agent-1.jsonl", "AWS access key id"),
        ("agent-2.jsonl", "GitHub token"),
        ("s.jsonl", "GitHub token"),
    ]
    nothing = creds.scan_transcripts(7, max_mb=0)
    assert (nothing.hits, nothing.scanned, nothing.total, nothing.stopped) == ([], 0, 3, True)
    rc = ctl._run(["creds", "scan"])
    out = capsys.readouterr().out
    assert rc == 1 and "scanned 3 transcript file(s) from the last 7 day(s)" in out
    assert "agent-2.jsonl" in out
    no_raw(out, repr(hits))


def sized_transcript(path: Path, kb: int, text: str = "") -> Path:
    """A transcript of about `kb` kilobytes (lines of filler), with `text` on the first line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = json.dumps({"type": "user", "message": {"content": "x" * 900}}) + "\n"
    lines = [json.dumps({"type": "user", "message": {"content": text}}) + "\n"]
    lines += [filler] * (kb * 1024 // len(filler))
    path.write_text("".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def small_budget(monkeypatch):
    """`creds scan` with a byte budget of a few hundred KB instead of 400 MB."""
    real = creds.scan_transcripts

    def use(mb: float) -> None:
        monkeypatch.setattr(creds, "scan_transcripts", lambda days: real(days, mb))

    return use


def test_the_budget_goes_to_main_sessions_first_and_the_result_says_how_much_was_covered(
    capsys, small_budget
):
    """Subagent files are several times more numerous than sessions: read newest first together, they used the whole
    budget and left the sessions of the week unread, and the scan still said 'no secrets found'."""
    root = paths.claude_home() / "projects" / "proj"
    now = time.time()
    for i in range(4):  # sessions, 1 to 4 days old; the secret is in the third one
        f = sized_transcript(
            root / f"s{i}.jsonl", 300, f"token {GH_TOKEN}" if i == 2 else "nothing here"
        )
        os.utime(f, (now - (i + 1) * 86400, now - (i + 1) * 86400))
    for i in range(
        4
    ):  # subagent files, all newer than every session; one of them holds a secret too
        f = sized_transcript(
            root / "s0" / "subagents" / f"agent-{i}.jsonl",
            300,
            f"key {AWS_LONG_ID}" if i == 3 else "nothing here",
        )
        os.utime(f, (now - 60 * (i + 1), now - 60 * (i + 1)))
    res = creds.scan_transcripts(7, max_mb=0.7)  # room for about 3 of the 8 files
    assert [Path(f).name for f, kind, n in res.hits] == ["s2.jsonl"]  # a session, not an agent file
    assert (res.main_scanned, res.main_total, res.sub_scanned, res.sub_total) == (3, 4, 0, 4)
    assert res.stopped and not res.complete and res.scanned == 3 and res.total == 8
    lines = res.coverage_lines()
    assert lines[0].startswith("scanned 3 of 8 transcript file(s), covering the last ")
    assert lines[0].endswith("day(s) of 7; stopped at the 0.7 MB budget")
    assert any(
        ln.strip().startswith("main sessions: 3 of 4 file(s), read back to 3.0") for ln in lines
    )
    assert any(ln.strip() == "subagent/workflow files: 0 of 4 file(s), none read" for ln in lines)
    # the command says so, and does not call the part it read a clean week
    small_budget(0.7)
    assert ctl._run(["creds", "scan"]) == 1
    text = capsys.readouterr().out
    assert "stopped at the 0.7 MB budget" in text and "s2.jsonl" in text
    no_raw(text, repr(res))


def test_a_complete_scan_with_nothing_found_says_so_plainly(capsys):
    transcript(paths.claude_home() / "projects" / "proj" / "s.jsonl", "nothing here")
    assert ctl._run(["creds", "scan"]) == 0
    text = capsys.readouterr().out
    assert "scanned 1 transcript file(s) from the last 7 day(s)" in text
    assert text.strip().endswith("no token-shaped secrets found")


def test_a_scan_that_stopped_at_its_budget_is_not_called_clean(capsys, small_budget):
    root = paths.claude_home() / "projects" / "proj"
    for i in range(3):
        sized_transcript(root / f"s{i}.jsonl", 300)
    small_budget(0.5)
    rc = ctl._run(["creds", "scan"])
    text = capsys.readouterr().out
    assert rc == 0  # nothing was found, and the exit status is about findings
    assert "stopped at the 0.5 MB budget" in text
    assert text.strip().endswith("no token-shaped secrets found in the part that was scanned")


def test_a_transcript_that_cannot_be_read_is_counted_and_reported(capsys, monkeypatch):
    root = paths.claude_home() / "projects" / "proj"
    transcript(root / "ok.jsonl", f"token {GH_TOKEN}")
    transcript(root / "locked.jsonl", "x")
    real_open = open

    def refuse(file, *args, **kwargs):
        if str(file).endswith("locked.jsonl"):
            raise PermissionError(13, "denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse)
    res = creds.scan_transcripts(7)
    assert (res.scanned, res.unreadable, res.complete) == (1, 1, False)
    assert "1 transcript file(s) could not be read" in res.coverage_lines()
    rc = ctl._run(["creds", "scan"])
    text = capsys.readouterr().out
    assert rc == 1 and "1 transcript file(s) could not be read" in text


def test_the_transcript_scan_opens_files_through_long_path(monkeypatch):
    """On Windows a deep workflow transcript is only openable in the extended-length form."""
    root = paths.claude_home() / "projects" / "proj"
    transcript(root / "s" / "subagents" / "workflows" / "wf_1" / "agent-1.jsonl", "nothing")
    seen: list[str] = []
    real = creds.usage.long_path
    monkeypatch.setattr(creds.usage, "long_path", lambda p, *a, **k: seen.append(str(p)) or real(p))
    assert creds.scan_transcripts(7).scanned == 1
    assert any(p.endswith("agent-1.jsonl") for p in seen)


# ---- a project's files are a repository's: nothing in them can hang the scan, exhaust memory or end it ------------


def make_project(tmp_path: Path, name: str, mcp=None, settings=None, local=None) -> Path:
    proj = tmp_path / name
    (proj / ".claude").mkdir(parents=True)
    for rel, data in (
        (".mcp.json", mcp),
        (".claude/settings.json", settings),
        (".claude/settings.local.json", local),
    ):
        if data is not None:
            (proj / rel).write_text(
                data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
            )
    return proj


def server_with(name: str = "API_TOKEN", value: str = BEARER) -> dict:
    return {"mcpServers": {"s": {"env": {name: value}}}}


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create symlinks")


def is_path(p: object, target: Path) -> bool:
    return isinstance(p, (str, os.PathLike)) and str(p) == str(target)


def test_a_project_file_that_is_not_a_regular_file_is_never_opened(tmp_path, monkeypatch):
    """A repository can commit `.mcp.json -> /dev/zero`, or a FIFO: stat says size 0, and a read never ends or blocks."""
    proj = make_project(tmp_path, "proj", mcp=server_with())
    claude_json({"projects": {str(proj): {}}})
    target = proj / ".mcp.json"
    real_lstat, real_open = os.lstat, os.open
    fifo = os.stat_result((stat.S_IFIFO | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    opened: list[str] = []

    def lstat(p, *a, **k):
        return fifo if is_path(p, target) else real_lstat(p, *a, **k)

    def opener(p, *a, **k):
        opened.append(str(p))
        return real_open(p, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(os, "lstat", lstat)
        m.setattr(os, "open", opener)
        res = creds.scan_config()
    assert res.findings == [] and str(target) not in opened
    assert res.notes == ["1 project file(s) are symlinks or special files and were not read"]


def test_a_windows_reparse_point_is_not_read_like_a_symlink(tmp_path, monkeypatch):
    """A junction, a OneDrive placeholder or a WSL special file is a regular file to S_ISREG but a reparse point."""
    from types import SimpleNamespace

    proj = make_project(tmp_path, "proj", mcp=server_with())
    claude_json({"projects": {str(proj): {}}})
    target = proj / ".mcp.json"
    real_lstat = os.lstat
    point = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x400 | 0x20)
    plain = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x20)  # just "archive"
    assert creds._is_link(point) and not creds._is_link(plain)
    assert not creds._is_link(SimpleNamespace(st_mode=stat.S_IFREG | 0o644))  # POSIX: no attributes
    with monkeypatch.context() as m:
        m.setattr(
            os, "lstat", lambda p, *a, **k: point if is_path(p, target) else real_lstat(p, *a, **k)
        )
        res = creds.scan_config()
    assert res.findings == []
    assert res.notes == ["1 project file(s) are symlinks or special files and were not read"]


def test_a_symlinked_project_file_or_folder_is_not_followed(tmp_path):
    real = make_project(
        tmp_path, "real", mcp=server_with()
    )  # the same content as a regular file is found
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "mcp.json").write_text(json.dumps(server_with()), encoding="utf-8")
    (elsewhere / "settings.json").write_text(
        json.dumps({"env": {"GH_TOKEN": GH_TOKEN}}), encoding="utf-8"
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    symlink_or_skip(linked / ".mcp.json", elsewhere / "mcp.json")
    symlink_or_skip(linked / ".claude", elsewhere)
    claude_json({"projects": {str(real): {}, str(linked): {}}})
    res = creds.scan_config()
    assert [(f.project, f.server) for f in res.findings] == [(str(real), "s")]
    assert res.notes == ["3 project file(s) are symlinks or special files and were not read"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_in_a_project_folder_does_not_block_the_scan(tmp_path):
    proj = make_project(tmp_path, "proj")
    os.mkfifo(proj / ".mcp.json")
    other = tmp_path / "other"
    other.mkdir()
    os.symlink(proj / ".mcp.json", other / ".mcp.json")  # a link to the FIFO
    claude_json({"projects": {str(proj): {}, str(other): {}}})
    result: list[creds.ConfigScan] = []
    t = threading.Thread(target=lambda: result.append(creds.scan_config()), daemon=True)
    t.start()
    t.join(15)
    assert not t.is_alive(), "the scan blocked on a FIFO"
    assert result[0].findings == []
    assert result[0].notes == ["2 project file(s) are symlinks or special files and were not read"]


def test_a_project_file_past_the_size_limit_is_noted_and_never_read_in_full(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "MAX_PROJECT_FILE_BYTES", 300)
    proj = make_project(tmp_path, "proj", mcp="[" + "0," * 2000 + "0]")  # 4 KB
    claude_json({"projects": {str(proj): {}}})
    res = creds.scan_config()
    assert res.findings == []
    assert res.notes == ["1 project file(s) larger than 300 bytes were not read"]
    # a device reports a size of 0: the read is bounded all the same
    real_fstat, real_read = os.fstat, os.read
    read_bytes: list[int] = []

    def liar(fd):
        st = real_fstat(fd)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, 0, 0, 0, 0))

    def counting_read(fd, n):
        data = real_read(fd, n)
        read_bytes.append(len(data))
        return data

    with monkeypatch.context() as m:
        m.setattr(os, "fstat", liar)
        m.setattr(os, "read", counting_read)
        res = creds.scan_config()
    assert res.notes == ["1 project file(s) larger than 300 bytes were not read"]
    assert (
        301 in read_bytes and max(read_bytes) == 301
    )  # the limit and one byte more, then it stops


def test_json_nested_past_what_python_parses_cannot_abort_the_scan(tmp_path):
    deep = "[" * 100_000 + "]" * 100_000
    bad = make_project(
        tmp_path, "bad", mcp=deep, settings=deep, local='{"env": {"A": ' + "1" * 5000 + "}}"
    )
    good = make_project(tmp_path, "good", mcp=server_with())
    (paths.claude_home() / "settings.json").write_text(
        json.dumps({"env": {"DB_PASSWORD": BEARER}}), encoding="utf-8"
    )
    claude_json({"projects": {str(bad): {}, str(good): {}}})
    res = creds.scan_config()
    assert [(f.project, f.name) for f in res.findings] == [
        ("", "DB_PASSWORD"),
        (str(good), "API_TOKEN"),
    ]
    assert res.unread == [] and res.notes == []


def test_a_user_level_file_nested_too_deep_is_reported_not_raised():
    paths.claude_global_json().write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == []
    assert skipped == [".claude.json is not valid JSON"]


def test_a_project_file_that_cannot_be_opened_is_counted_and_the_scan_goes_on(
    tmp_path, monkeypatch
):
    locked = make_project(tmp_path, "locked", mcp=server_with())
    good = make_project(tmp_path, "good", mcp=server_with())
    claude_json({"projects": {str(locked): {}, str(good): {}}})
    real_open = os.open

    def deny(p, *a, **k):
        if is_path(p, locked / ".mcp.json"):
            raise PermissionError(13, "denied")
        return real_open(p, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(os, "open", deny)
        res = creds.scan_config()
    assert [f.project for f in res.findings] == [str(good)]
    assert res.notes == ["1 project file(s) could not be read"]


def test_binary_garbage_and_a_nul_in_a_project_key_do_not_stop_the_scan(tmp_path):
    """A NUL in one key made stat() raise ValueError, which dropped every project after it and the user's own files."""
    junk = make_project(tmp_path, "junk")
    (junk / ".mcp.json").write_bytes(b"\xff\xfe\x00\x01" + os.urandom(200))
    good = make_project(tmp_path, "good", mcp=server_with())
    (paths.claude_home() / "settings.json").write_text(
        json.dumps({"env": {"DB_PASSWORD": BEARER}}), encoding="utf-8"
    )
    claude_json({"projects": {str(tmp_path / "bad\x00key"): {}, str(junk): {}, str(good): {}}})
    res = creds.scan_config()
    assert [(f.project, f.name) for f in res.findings] == [
        ("", "DB_PASSWORD"),
        (str(good), "API_TOKEN"),
    ]
    assert res.unread == [] and res.notes == []  # a repository's own bad JSON is its business


def test_one_project_failing_unexpectedly_is_noted_and_the_scan_goes_on(tmp_path, monkeypatch):
    boom = make_project(tmp_path, "bad", mcp='{"marker-of-the-bad-file": 1}')
    good = make_project(tmp_path, "good", mcp=server_with())
    claude_json({"projects": {str(boom): {}, str(good): {}}})
    real = creds._parse

    def parse(data):
        if b"marker-of-the-bad-file" in data:
            raise MemoryError
        return real(data)

    monkeypatch.setattr(creds, "_parse", parse)
    res = creds.scan_config()
    assert [f.project for f in res.findings] == [str(good)]
    assert res.notes == ["1 project file(s) could not be scanned (error)"]


def test_a_scan_that_dies_says_so_instead_of_reporting_none(monkeypatch):
    claude_json(project_with({"aws-api": {"env": aws_env()}}))

    def die(data):
        raise MemoryError

    monkeypatch.setattr(creds, "_parse", die)
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == []
    assert skipped == [".claude.json caused an error (MemoryError)"]
    c = check("credentials")
    assert c.status == WARN and ".claude.json caused an error (MemoryError)" in c.detail


def test_the_users_own_files_are_scanned_before_any_projects(tmp_path, monkeypatch):
    (paths.claude_home() / "settings.json").write_text(
        json.dumps({"env": {"DB_PASSWORD": BEARER}}), encoding="utf-8"
    )
    proj = make_project(tmp_path, "p", mcp=server_with())
    claude_json({"projects": {str(proj): {}}})
    monkeypatch.setattr(creds, "MAX_FINDINGS", 1)
    res = creds.scan_config()
    assert [(f.file, f.project) for f in res.findings] == [("settings.json", "")]
    assert res.capped


def test_a_user_settings_file_that_is_not_a_regular_file_is_reported():
    (paths.claude_home() / "settings.json").mkdir()
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == []
    assert len(skipped) == 1 and skipped[0].startswith("settings.json ")
    assert check("credentials").status == WARN


def test_the_users_own_settings_may_be_a_symlink_to_a_regular_file(tmp_path):
    """Dotfile managers link settings.json; only a project folder's files are never followed."""
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"env": {"DB_PASSWORD": BEARER}}), encoding="utf-8")
    symlink_or_skip(paths.claude_home() / "settings.json", real)
    assert [f.name for f in creds.scan_config_secrets()] == ["DB_PASSWORD"]


# ---- the caps are said, not silent ------------------------------------------------------------------------------


def test_a_scan_stopped_by_the_project_cap_says_so_instead_of_none(tmp_path, monkeypatch, capsys):
    """The secret is in the oldest of three projects and only the newest two are read: 'none' would be false comfort."""
    projs = [make_project(tmp_path, f"p{i}") for i in range(3)]
    (projs[0] / ".mcp.json").write_text(json.dumps(server_with()), encoding="utf-8")
    claude_json({"projects": {str(p): {} for p in projs}})
    monkeypatch.setattr(creds, "MAX_PROJECT_DIRS", 2)
    res = creds.scan_config()
    note = "scan stopped after 2 of 3 projects (only the 2 most recent are read)"
    assert res.findings == [] and res.notes == [note]
    c = check("MCP literal secrets")
    assert c.status == INFO
    assert c.detail == f"none (not everything was scanned: {note})"
    assert ctl._run(["creds", "doctor"]) == 0
    assert note in capsys.readouterr().out


def test_a_scan_that_ran_out_of_time_says_how_far_it_got(tmp_path, monkeypatch):
    proj = make_project(tmp_path, "proj", mcp=server_with())
    claude_json({"projects": {str(proj): {}}})
    monkeypatch.setattr(creds, "SCAN_SECONDS", -1)
    res = creds.scan_config()
    assert res.findings == []
    assert res.notes == ["scan stopped after 0 of 1 projects (time limit -1 s)"]


def test_the_cap_on_findings_is_reported(capsys, monkeypatch):
    claude_json(project_with({"aws-api": {"env": aws_env()}}))  # three findings
    monkeypatch.setattr(creds, "MAX_FINDINGS", 2)
    res = creds.scan_config()
    assert len(res.findings) == 2 and res.capped
    assert res.notes == ["findings are capped at 2; there may be more"]
    c = check("MCP literal secrets")
    assert c.status == WARN and c.detail.startswith("2+ literal secret(s) found: ")
    assert "findings are capped at 2" in c.detail
    ctl._run(["creds", "doctor"])
    assert "2+ literal secret(s) in Claude's configuration" in capsys.readouterr().out


def test_a_complete_scan_has_no_notes(tmp_path):
    proj = make_project(tmp_path, "proj", mcp=server_with())
    claude_json(
        {"projects": {str(proj): {}, str(tmp_path / "gone"): {}}}
    )  # a deleted folder is no gap
    res = creds.scan_config()
    assert len(res.findings) == 1 and res.notes == [] and not res.capped


# ---- what is a literal secret ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("API_KEY_FILE", r"C:\keys\a.pem"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/home/u/.config/gcloud/key.json"),
        ("AWS_SHARED_CREDENTIALS_FILE", r"C:\Users\x\.aws\credentials"),
        ("DB_PASSWORD_FILE", "/run/secrets/db_password"),
        ("TOKEN_CACHE_DIR", "~/.cache/tokens"),
        ("SECRET_FILE", "secrets/gh.txt"),
        ("SECRET_FILE", "key.pem"),
        ("SECRET_NAME", "prod/db/credentials"),
        ("SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db-AbCdEf"),
        ("TOKEN_URL", "https://x/y"),
        ("TOKEN_ENDPOINT", "https://auth.internal.test/oauth/token"),
        ("GITHUB_TOKEN", "keychain:claude-hr/github"),
        ("GITHUB_TOKEN", "vault:secret/data/gh#token"),
        ("GITHUB_TOKEN", "bw://item/gh"),
        ("GITHUB_TOKEN", "aws-sm://prod/gh"),
        ("GITHUB_TOKEN", "file:/run/secrets/gh"),
        ("GITHUB_TOKEN", "op://Private/GitHub/token"),
        ("PASSWORD_PROMPT", "Enter password for user"),
        ("TOKEN_LIFETIME", "3600 seconds"),
        ("TOKEN_ENDPOINT_AUTH_METHOD", "client_secret_basic"),
        ("API_KEY_HEADER", "Authorization"),
        (
            "SESSION_ID",
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        ),  # a UUID under a name that says nothing
        ("GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567"),  # a hash
        ("INTEGRITY", "sha512-" + "AbCd0123" * 11),
        ("BUILD_LABEL", "ThisIsAVeryLongConfigurationValue123"),  # camelCase, not random
        ("DATABASE_URL", "postgres://host/db"),  # a URL without a user
        ("DATABASE_URL", "postgres://user@host/db"),
        ("DATABASE_URL", "postgres://user:${DB_PASS}@host/db"),  # a reference in the password place
        ("API_TOKEN", "abc1234"),  # under 8 characters
    ],
)
def test_ordinary_values_are_not_literal_secrets(name, value):
    assert creds._classify(name, value) is None


def test_ordinary_mcp_configuration_stays_clean(tmp_path):
    proj = tmp_path / "token-secret-service"  # a project folder that says 'token' and 'secret'
    proj.mkdir()
    server = {
        "command": "node",
        "args": [
            str(proj / "server.js"),
            "--token-file",
            r"C:\keys\gh_token.txt",
            "--cache",
            "tok/cache",
        ],
        "env": {
            "API_KEY_FILE": r"C:\keys\a.pem",
            "TOKEN_CACHE_DIR": str(proj / "cache"),
            "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db-AbCdEf",
            "TOKEN_URL": "https://x/y",
            "GITHUB_TOKEN": "keychain:claude-hr/github",
        },
        "url": "https://mcp.internal.test/sse",
    }
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": server}}), encoding="utf-8")
    claude_json(
        {"mcpServers": {"s": server}, "projects": {str(proj): {"mcpServers": {"s": server}}}}
    )
    res = creds.scan_config()
    assert res.findings == [] and res.notes == []
    assert check("MCP literal secrets").detail == "none"


def test_real_secrets_are_still_found_under_any_name_or_path():
    b64 = fake("", 38, string.ascii_letters + string.digits + "/+", seed=9) + "/+"
    found = {
        "API_TOKEN": BEARER,
        "SOMETHING": GH_TOKEN,  # a token's shape under a name that says nothing
        "NEUTRAL": AWS_LONG_ID,
        "ANOTHER": fake("", 40, seed=6),  # a long random run
        "API_KEY": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",  # a UUID under a secret-looking name is a key
        "SECRET_KEY": b64,  # base64 can contain '/': it is not a path
        "DB_PASSWORD": "tmp/" + BEARER,
        "GOOGLE_API_KEY_FILE": fake("AIza", 35),
    }
    kinds = {k: (creds._classify(k, v) or ("MISSED",))[0] for k, v in found.items()}
    assert kinds == {
        "API_TOKEN": "token",
        "SOMETHING": "GitHub token",
        "NEUTRAL": "AWS access key id",
        "ANOTHER": "high-entropy string",
        "API_KEY": "API key",
        "SECRET_KEY": "secret",
        "DB_PASSWORD": "password",
        "GOOGLE_API_KEY_FILE": "Google API key",  # a token's shape wins over a name that says 'file'
    }


def test_passwords_in_connection_strings_are_found_wherever_they_are():
    claude_json(
        {
            "mcpServers": {
                "pg": {
                    "command": "npx",
                    "args": [
                        "-y",
                        "@modelcontextprotocol/server-postgres",
                        f"postgresql://admin:{DB_PW}@db.internal.test/prod",
                    ],
                },
                "app": {
                    "env": {
                        "DATABASE_URL": f"postgresql://svc:{DB_PW}@db.internal.test/app",
                        "REDIS_URL": f"redis://:{DB_PW}@cache.internal.test",
                        "PLAIN_URL": "postgresql://db.internal.test/app",
                        "REF_URL": "postgresql://svc:${DB_PASSWORD}@db.internal.test/app",
                    },
                    "command": f"psql postgres://svc:{DB_PW}@db.internal.test/app",
                },
            }
        }
    )
    found = creds.scan_config_secrets()
    assert [(f.server, f.field, f.name, f.kind) for f in found] == [
        ("pg", "args", "[2]", "password"),
        ("app", "env", "DATABASE_URL", "password"),
        ("app", "env", "REDIS_URL", "password"),
        ("app", "command", "command", "password"),
    ]
    text = "\n".join(creds.format_findings(found)) + repr(found)
    no_raw(text)
    assert "admin" not in text and "svc" not in text and "internal.test" not in text


def test_opaque_authorization_headers_are_found_in_args_and_variables():
    claude_json(
        {
            "mcpServers": {
                "remote": {
                    "command": "npx",
                    "args": [
                        "mcp-remote",
                        "https://mcp.internal.test/sse",
                        "--header",
                        f"Authorization: Bearer {OPAQUE}",
                        "-H",
                        f"X-Api-Key:{OPAQUE}",
                        f"--header=Authorization:Basic {OPAQUE}",
                        "--header",
                        "Accept: application/json",
                        "--header",
                        "Authorization: Bearer ${MCP_TOKEN}",
                    ],
                    "env": {"AUTH_HEADER": f"Bearer {OPAQUE}", "NOTE": "Bearer authentication"},
                }
            }
        }
    )
    found = creds.scan_config_secrets()
    assert [(f.field, f.name, f.kind) for f in found] == [
        ("env", "AUTH_HEADER", "bearer token"),
        ("args", "Authorization", "bearer token"),
        ("args", "X-Api-Key", "API key"),
        ("args", "header", "basic auth credentials"),
    ]
    no_raw(repr(found), "\n".join(creds.format_findings(found)))


# ---- untrusted text never reaches the terminal as an escape ------------------------------------------------------


def test_masked_values_and_names_cannot_inject_terminal_escapes(capsys):
    """The first characters of a value are printed: an ESC in them cleared the screen of whoever ran `creds doctor`."""
    evil = "\x1b[2J\x1b[3J" + "A" * 20
    claude_json(
        {
            "projects": {
                "C:/p\x1b[0m": {
                    "mcpServers": {
                        "srv\x1b[2J": {"env": {"X_TOKEN": evil, "Y\x1b[2J_TOKEN": BEARER}}
                    }
                }
            }
        }
    )
    found = creds.scan_config_secrets()
    assert len(found) == 2
    text = "\n".join(creds.format_findings(found))
    c = check("MCP literal secrets")
    assert "\x1b" not in text + c.line() + repr(c)
    assert r"value: \x1b[2J…(28 chars)" in text  # written out, so it can be read
    assert r"\x1b[2J" in found[1].name and "\x1b" not in found[1].where
    assert ctl._run(["creds", "doctor"]) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_bidi_overrides_in_config_text_are_written_out():
    claude_json({"mcpServers": {"s\u202eevil": {"env": {"API_TOKEN\u202e": BEARER}}}})
    (f,) = creds.scan_config_secrets()
    assert "\u202e" not in f.where and r"\u202e" in f.where


def test_the_weekly_jobs_alert_and_log_say_when_the_scan_did_not_cover_everything(monkeypatch):
    res = creds.TranscriptScan(
        [("s.jsonl", "GitHub token", 1)],
        7,
        400,
        scanned=10,
        total=50,
        main_scanned=10,
        main_total=20,
        stopped=True,
        horizon_days=0.5,
        main_back_days=2.0,
    )
    monkeypatch.setattr(creds, "scan_transcripts", lambda days, max_mb=400: res)
    alerts: list = []
    monkeypatch.setattr(ctl.monitor, "deliver", lambda cfg, alert: alerts.append(alert))
    monkeypatch.setattr(ctl.maintenance, "plan", lambda cfg: None)
    monkeypatch.setattr(ctl.maintenance, "apply", lambda p: (0, 0.0))
    monkeypatch.setattr(ctl.Headroom, "binary", lambda self: None)
    assert ctl._run(["job", "maintain"]) == 0
    (alert,) = alerts
    assert alert.body == (
        "1 finding(s) in 10 of 50 files (the scan did not cover everything); "
        "run `claude-hr-ctl creds scan`"
    )
    log = (paths.logs_dir() / "job-maintain.log").read_text(encoding="utf-8")
    assert (
        "transcript scan: scanned 10 of 50 transcript file(s), covering the last 0.5 day(s) of 7"
        in log
    )


def test_hostile_values_cannot_make_the_classifier_slow():
    """The values of a cloned repo's .mcp.json are fed to the patterns: none may backtrack for long on a 4 KB run."""
    runs = [
        "a" * 4090 + "://",
        "a/" * 2040 + "x",
        "-_" * 2045,
        "A1" * 2045,
        " " * 4090 + "x",
        "Authorization:" + " " * 4000,
        "://" * 1365,
        "a:" * 2045,
        "x@" * 2045,
        "http://" + "a" * 4000 + ":",
        "?a=" * 1360,
        "Bearer " + "a" * 4000,
        "." * 4090 + "pem",
    ]
    t0 = time.monotonic()
    for _ in range(10):
        for v in runs:
            for name in ("API_TOKEN", "", "Authorization", "X"):
                creds._classify(name, v)
    assert time.monotonic() - t0 < 10
