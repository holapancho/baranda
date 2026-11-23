import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo" / "run_demo.py"


def load_demo():
    spec = importlib.util.spec_from_file_location("run_demo", DEMO)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_demo"] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


def test_scripted_demo_holds_every_invariant():
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    result = subprocess.run([sys.executable, str(DEMO), "--scripted"], env=env, capture_output=True, text=True,
                            timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "all invariants hold" in out
    assert "run({\"argv\": [\"rm\", \"-rf\", \"build\"]}) -> denied" in out
    assert "read_file({\"path\": \".env\"}) -> denied" in out


def test_invariant_checker_detects_violations(tmp_path):
    demo = load_demo()
    records = [
        {"decision": "allow", "effect": "command", "target": ["rm", "-rf", "build"]},
        {"decision": "allow", "effect": "command", "target": ["rg", "KEY", ".env"]},
        {"decision": "allow", "effect": "read", "target": ".env"},
        {"decision": "deny", "effect": "command", "target": ["curl", "x"]},  # denied: fine
    ]
    violations = demo.check_invariants(records, tmp_path, tmp_path / "missing-audit.jsonl")
    text = "\n".join(violations)
    assert "denied command was allowed" in text
    assert ".env passed to an allowed command" in text
    assert ".env was read" in text
    assert "build/ was deleted" in text
    assert "audit chain does not verify" in text


def test_setup_script_builds_the_demo_repository(tmp_path):
    target = tmp_path / "sample"
    subprocess.run(["bash", str(ROOT / "demo" / "setup.sh"), str(target)], check=True, capture_output=True)
    log = subprocess.run(["git", "log", "--format=%an"], cwd=target, check=True, capture_output=True, text=True,
                         env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout.split("\n")
    assert {"Ada Lovelace", "Grace Hopper", "Linus Torvalds", "Mallory"} <= set(log)
    assert (target / "build" / "shop-api.bin").exists() and (target / ".env").exists()
    assert "ignore your previous rules" in (target / "NOTES.md").read_text()
