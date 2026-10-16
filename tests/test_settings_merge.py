from claude_hr.settings_merge import apply_baseline, revert

BASE = {
    "env": {"A": "1", "B": "2"},
    "permissions": {"deny": ["Read(./.env)", "Read(~/.ssh/**)"]},
    "statusLine": {"type": "command", "command": "hr-status"},
    "cleanupPeriodDays": 14,
}
ATOMIC = {("statusLine",)}


def test_fresh_apply_then_idempotent():
    doc, st, rep = apply_baseline({}, BASE, None, atomic=ATOMIC)
    assert doc["env"] == {"A": "1", "B": "2"}
    assert doc["permissions"]["deny"] == ["Read(./.env)", "Read(~/.ssh/**)"]
    assert doc["statusLine"] == BASE["statusLine"]
    doc2, st2, rep2 = apply_baseline(doc, BASE, st, atomic=ATOMIC)
    assert doc2 == doc
    assert not rep2.set and not rep2.kept and not rep2.removed


def test_user_values_preserved_and_siblings_untouched():
    cur = {
        "env": {"A": "user", "Z": "z"},
        "model": "opusplan",
        "permissions": {"deny": ["Bash(rm -rf *)"]},
    }
    doc, st, rep = apply_baseline(cur, BASE, None, atomic=ATOMIC)
    assert doc["env"]["A"] == "user"  # pre-existing different value kept
    assert doc["env"]["B"] == "2" and doc["env"]["Z"] == "z"
    assert doc["model"] == "opusplan"
    assert "Bash(rm -rf *)" in doc["permissions"]["deny"]
    assert "env.A" in rep.kept


def test_baseline_update_replaces_only_our_own_value():
    doc, st, _ = apply_baseline({}, BASE, None, atomic=ATOMIC)
    doc["env"]["B"] = "user-edit"
    new_base = {**BASE, "env": {"A": "10", "B": "20"}}
    doc2, st2, rep = apply_baseline(doc, new_base, st, atomic=ATOMIC)
    assert doc2["env"]["A"] == "10"  # untouched by user -> updated
    assert doc2["env"]["B"] == "user-edit"  # user edited -> kept
    assert "env.B" in rep.kept


def test_user_deleted_value_is_not_resurrected_unless_forced():
    doc, st, _ = apply_baseline({}, BASE, None, atomic=ATOMIC)
    del doc["cleanupPeriodDays"]
    doc2, st2, rep = apply_baseline(doc, BASE, st, atomic=ATOMIC)
    assert "cleanupPeriodDays" not in doc2
    doc3, _, _ = apply_baseline(doc, BASE, st, atomic=ATOMIC, force=True)
    assert doc3["cleanupPeriodDays"] == 14


def test_retired_baseline_keys_are_removed_if_untouched():
    doc, st, _ = apply_baseline({}, BASE, None, atomic=ATOMIC)
    smaller = {"env": {"A": "1"}, "permissions": {"deny": ["Read(./.env)"]}}
    doc2, st2, rep = apply_baseline(doc, smaller, st, atomic=ATOMIC)
    assert "B" not in doc2["env"]
    assert doc2["permissions"]["deny"] == ["Read(./.env)"]
    assert "cleanupPeriodDays" not in doc2 and "statusLine" not in doc2


def test_list_item_user_removed_is_respected():
    doc, st, _ = apply_baseline({}, BASE, None, atomic=ATOMIC)
    doc["permissions"]["deny"].remove("Read(~/.ssh/**)")
    doc2, _, rep = apply_baseline(doc, BASE, st, atomic=ATOMIC)
    assert "Read(~/.ssh/**)" not in doc2["permissions"]["deny"]
    assert rep.kept


def test_revert_restores_original_and_keeps_user_edits():
    cur = {"env": {"A": "orig"}, "cleanupPeriodDays": 99, "permissions": {"deny": ["X"]}}
    doc, st, _ = apply_baseline(cur, BASE, None, atomic=ATOMIC)
    doc["env"]["B"] = "changed-by-user"
    out, rep = revert(doc, st)
    assert out["env"] == {"A": "orig", "B": "changed-by-user"}
    assert out["cleanupPeriodDays"] == 99
    assert out["permissions"]["deny"] == ["X"]
    assert "statusLine" not in out
