import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / "src" / "claude_hr" / "data" / "workflows" / "hr-review-changes.js"
HARNESS = ROOT / "tests" / "workflow_harness.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run(scenario="normal", args=None):
    cmd = ["node", str(HARNESS), str(WF), scenario]
    if args is not None:
        cmd.append(json.dumps(args))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_meta_is_a_pure_literal():
    text = WF.read_text(encoding="utf-8")
    meta = text[text.index("export const meta") : text.index("\n}\n", text.index("export const meta")) + 3]
    assert "${" not in meta and "`" not in meta, "meta must be a pure literal (no interpolation)"
    assert "(" not in meta, "meta must not contain calls"
    name = re.search(r"name:\s*'([^']+)'", meta).group(1)
    assert name == WF.stem, "file name must match meta.name so the saved workflow resolves"
    for title in re.findall(r"title:\s*'([^']+)'", meta):
        assert f"phase('{title}')" in text, f"meta phase {title!r} has no matching phase() call"


@needs_node
def test_survivors_are_deduped_ranked_and_the_fake_is_refuted():
    out = run()
    res = out["result"]
    titles = [f["title"] for f in res["confirmed"]]
    assert "fake bug" not in titles  # both skeptics refuted it (2 of 2 is a majority)
    assert titles[0] == "real bug"  # highest severity first
    a10 = [f for f in res["confirmed"] if f["file"] == "a.py" and f["line"] == 10]
    assert len(a10) == 1 and "security" in a10[0].get("alsoFoundBy", [])  # same spot from two dimensions -> one finding
    assert "minor" in titles
    assert res["markdown"].startswith("- [high] a.py:10")
    assert out["calls"].count("scope") == 1
    assert sum(c.startswith("find:") for c in out["calls"]) == 5
    assert sum(c.startswith("verify:") for c in out["calls"]) == (3 + 1) * 2  # 4 findings x 2 skeptics


@needs_node
def test_nothing_to_review_stops_before_spawning_finders():
    out = run("empty")
    assert out["result"]["confirmed"] == []
    assert not any(c.startswith("find:") for c in out["calls"])


@needs_node
def test_args_select_dimensions_votes_and_cap():
    out = run(args={"dimensions": ["correctness"], "votes": 3, "maxPerDimension": 2})
    assert sum(c.startswith("find:") for c in out["calls"]) == 1
    assert sum(c.startswith("verify:") for c in out["calls"]) == 2 * 3
    assert any("not checked" in m for m in out["logs"]), "dropping findings must be logged, not silent"


@needs_node
def test_findings_are_kept_but_flagged_when_every_skeptic_fails():
    out = run("skeptics-fail", {"dimensions": ["correctness"]})
    assert out["result"]["confirmed"], "an unverifiable finding must not vanish silently"
    assert all(f["answered"] == 0 for f in out["result"]["confirmed"])
    assert any("could not be verified" in m for m in out["logs"])


def _repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def test_workflows_install_copies_into_the_project_and_never_clobbers(sandbox, tmp_path):
    from claude_hr import config, ctl

    repo = _repo(tmp_path)
    ns = type("A", (), {"action": "install", "dir": str(repo), "force": False})()
    assert ctl.cmd_workflows(ns, config.load()) == 0
    installed = repo / ".claude" / "workflows" / "hr-review-changes.js"
    assert installed.read_text(encoding="utf-8") == WF.read_text(encoding="utf-8")
    installed.write_text("// my local edit\n", encoding="utf-8")
    assert ctl.cmd_workflows(ns, config.load()) == 0
    assert installed.read_text(encoding="utf-8") == "// my local edit\n"  # a differing copy is kept
    ns.force = True
    ctl.cmd_workflows(ns, config.load())
    assert installed.read_text(encoding="utf-8") == WF.read_text(encoding="utf-8")


def test_workflows_list_mentions_the_review_workflow(sandbox, capsys):
    from claude_hr import config, ctl

    ns = type("A", (), {"action": "list", "dir": None, "force": False})()
    assert ctl.cmd_workflows(ns, config.load()) == 0
    assert "hr-review-changes" in capsys.readouterr().out
