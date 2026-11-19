import json

import pytest

from baranda.guard import AuditError, AuditLog, Denied, Guard, verify


def test_decisions_are_recorded(guard, audit):
    guard.run(["git", "status"])
    with pytest.raises(Denied):
        guard.run(["rm", "-rf", "build"])

    allow, deny = audit.records[-2:]
    assert allow["decision"] == "allow" and allow["outcome"] == "ok" and allow["target"] == ["git", "status"]
    assert deny["decision"] == "deny" and "rm" in deny["reason"]
    assert allow["policy"] == deny["policy"] == guard.policy.hash
    assert deny["prev"] == allow["hash"]


def test_secrets_are_redacted_in_audit(guard, audit):
    with pytest.raises(Denied):
        guard.run(["curl", "-H", "Authorization: Bearer abc123", "https://x"])
    assert "abc123" not in json.dumps(audit.records[-1])


def test_chain_verifies_and_detects_tampering(repo, policy_dir, tmp_path):
    path = tmp_path / "audit.jsonl"
    guard = Guard(repo, [policy_dir / "connector.yaml"], audit=AuditLog(path))
    guard.run(["git", "status"])
    with pytest.raises(Denied):
        guard.run(["rm", "x"])
    guard.read("README.md")
    assert verify(path) == 3

    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["decision"] = "allow"
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(AuditError, match="line 2"):
        verify(path)


def test_chain_continues_across_restarts(repo, policy_dir, tmp_path):
    path = tmp_path / "audit.jsonl"
    Guard(repo, [policy_dir / "connector.yaml"], audit=AuditLog(path)).run(["git", "status"])
    Guard(repo, [policy_dir / "connector.yaml"], audit=AuditLog(path)).run(["git", "status"])
    assert verify(path) == 2


def test_deleting_a_line_is_detected(repo, policy_dir, tmp_path):
    path = tmp_path / "audit.jsonl"
    guard = Guard(repo, [policy_dir / "connector.yaml"], audit=AuditLog(path))
    for _ in range(3):
        guard.run(["git", "status"])
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(AuditError, match="chain broken"):
        verify(path)
