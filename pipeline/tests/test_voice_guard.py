import json, os, subprocess, sys, tempfile
from pathlib import Path
import pytest

GUARD = Path(__file__).resolve().parents[1] / "hooks" / "voice_run_guard.py"
WS = "/Users/rshao/work/context-infrastructure"


FAKE_HOME = Path(tempfile.mkdtemp(prefix="aha_guard_home_"))


def run(tool, inp, env=True, raw=None):
    (FAKE_HOME / ".zshrc").write_text("alias kprodx='kubectl --context prod-x'\nalias kdevy=\"kubectl --context dev-y\"\n")
    e = {**os.environ, "AHA_VOICE_WORKSPACE": WS, "HOME": str(FAKE_HOME)}
    e.pop("AHA_VOICE_RUN", None)
    if env: e["AHA_VOICE_RUN"] = "1"
    payload = raw if raw is not None else json.dumps({"tool_name": tool, "tool_input": inp, "cwd": WS})
    return subprocess.run([sys.executable, str(GUARD)], input=payload, capture_output=True, text=True, env=e)


@pytest.mark.parametrize("cmd", [
    "kubectl get pods", "kdevy get po", "aws s3 ls", "helm list", "k get ns", "kprodx delete pod x",
    "cd /tmp && kubectl get po", "ls | xargs kubectl get", "/usr/local/bin/kubectl get po", "FOO=1 aws sts get-caller-identity",
    "echo hi; kprodx get po", "bash -c 'kubectl get po'", "sudo ls", "git push origin main",
    "echo a > /tmp/x", "touch /tmp/x", "rm -rf ../other", "cp a.md ~/Desktop/a.md", "echo a >/tmp/y", "echo $HOME | tee $HOME/x",
])
def test_bash_blocked(cmd):
    r = run("Bash", {"command": cmd})
    assert r.returncode == 2, cmd
    assert "BLOCKED" in r.stderr


@pytest.mark.parametrize("cmd", [
    "openspec list", "ls -la contexts", "cat README.md", "git status", "mkdir -p openspec/changes/x",
    "echo hi > contexts/scratch_voice_test.md", f"cp a.md {WS}/b.md", "grep -rn kube rules | head", "curl -k https://x.example", "ls /usr/bin",
])
def test_bash_allowed(cmd):
    r = run("Bash", {"command": cmd})
    assert r.returncode == 0, (cmd, r.stderr)


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path")])
def test_write_outside_blocked(tool, key):
    assert run(tool, {key: "/tmp/x"}).returncode == 2
    assert run(tool, {key: "/Users/rshao/.zshrc"}).returncode == 2
    assert run(tool, {key: f"{WS}/../evil.md"}).returncode == 2
    assert run(tool, {key: f"{WS}/.git/config"}).returncode == 2
    assert run(tool, {key: f"{WS}/.claude/settings.json"}).returncode == 2


def test_write_inside_allowed():
    assert run("Write", {"file_path": f"{WS}/contexts/scratch_voice_test.md", "content": "x"}).returncode == 0
    assert run("Edit", {"file_path": "contexts/x.md"}).returncode == 0       # relative to cwd=WS
    assert run("Read", {"file_path": "/etc/hosts"}).returncode == 0


def test_mcp_blocked_and_inactive_without_env_and_fail_closed():
    assert run("mcp__slack__slack_send_message", {}).returncode == 2
    assert run("Bash", {"command": "kubectl get po"}, env=False).returncode == 0   # only active for voice runs
    assert run("", {}, raw="not json").returncode == 2
