"""Self-update: Zax proposes a change to its OWN source code and only ever ships it
through a real gate — never straight to the running app.

Pipeline:
  1. Create an isolated git WORKTREE on a new branch (built from committed HEAD —
     never touches the live app's working directory or process while it runs).
  2. An LLM tool-loop reads/writes files under zax/ and tests/ inside that worktree
     to achieve the goal.
  3. The full test suite runs INSIDE the worktree. If it fails, the attempt is
     discarded — nothing is committed, nothing reaches the Founder.
  4. If tests pass, the change is committed on its branch and an approval request
     is raised (the same Approve/Deny gate already used for shell/code) — the
     Founder reviews the diff before anything merges.
  5. Only on explicit approval does it merge into main and restart the live
     process (launchd/KeepAlive relaunches it with the new code). The pre-merge
     commit hash is always reported, so a bad merge is one `git reset --hard` away
     from undone.
"""
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import config, db, llm

ALLOWED_PREFIXES = ("zax", "tests")  # only these subtrees may be read/written


async def _git(*args: str, cwd: Path, timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"git {' '.join(args)} timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace").strip()


def _safe_source_path(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, restricted to zax/ and tests/ — refuses path
    traversal, absolute paths, and anything touching .git or outside those trees."""
    rel = rel.strip().lstrip("/")
    if not rel or ".." in Path(rel).parts or Path(rel).is_absolute():
        raise ValueError("invalid path")
    parts = Path(rel).parts
    if parts[0] not in ALLOWED_PREFIXES or ".git" in parts:
        raise ValueError(f"path must be under {'/'.join(ALLOWED_PREFIXES)}/ — refused: {rel}")
    p = (root / rel).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("path escapes the repo")
    return p


def _list_source(root: Path, rel: str) -> str:
    try:
        base = _safe_source_path(root, rel or "zax")
    except ValueError as e:
        return f"Error: {e}"
    if not base.exists():
        return f"No such path: {rel}"
    if base.is_file():
        return f"{rel} ({base.stat().st_size} bytes)"
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in base.iterdir()
                     if p.name != "__pycache__" and not p.name.endswith(".pyc"))
    return "\n".join(entries) or "(empty)"


def _read_source(root: Path, rel: str) -> str:
    try:
        p = _safe_source_path(root, rel)
    except ValueError as e:
        return f"Error: {e}"
    if not p.exists() or not p.is_file():
        return f"No such file: {rel}"
    return p.read_text(errors="replace")[:12000]


def _write_source(root: Path, rel: str, content: str) -> str:
    try:
        p = _safe_source_path(root, rel)
    except ValueError as e:
        return f"Error: {e}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} chars to {rel}"


def _delete_source(root: Path, rel: str) -> str:
    try:
        p = _safe_source_path(root, rel)
    except ValueError as e:
        return f"Error: {e}"
    if not p.exists():
        return f"No such file: {rel}"
    p.unlink()
    return f"Deleted {rel}"


_TOOL_SPECS = """You may use tools. To call one, respond with ONLY a JSON object:
  {"tool": "<name>", "args": {...}}
Available tools (paths are relative to the repo root, e.g. "zax/agents.py"):
  list_source   {"path": str}                 -> list files under a directory
  read_source   {"path": str}                 -> read a file's full contents
  write_source  {"path": str, "content": str}  -> overwrite/create a file with the given COMPLETE content
  delete_source {"path": str}                  -> delete a file
Only zax/ and tests/ are writable. Read enough of the surrounding code to make a
correct, minimal change — don't guess at code you haven't read. When the change is
complete, respond with ONLY: {"final": "<one paragraph: what changed and why>"}"""

_SYSTEM = """You are Zax's self-improvement engineer — you modify Zax's OWN source code.

GOAL: {goal}

Make the SMALLEST correct change that achieves the goal. Preserve existing style and
conventions. Do not touch tests unless the goal requires it or your change needs test
coverage. Never weaken the approval gate, the self-check loop, or safety guards
(path restrictions, command approval, encryption) — if the goal seems to ask for that,
make the safest interpretation instead and say so in your final summary.

{tools}"""


_ONE_UPDATE = asyncio.Lock()


# Live progress so the UI can show what a self-update is doing (and the endpoint can
# tell the truth about "busy" instead of a blind "on it").
_PROGRESS: dict = {"active": False, "goal": "", "phase": "idle", "started": 0.0}


def status() -> dict:
    """Current self-update progress (for /api/self-update/status + the Bridge indicator)."""
    return dict(_PROGRESS)


def recover() -> None:
    """On boot, prune any worktree/branch stranded by a crash or a mid-run restart —
    so a leftover never wedges the 'one at a time' lock or clutters the repo."""
    import subprocess
    root = str(config.ROOT)
    try:
        subprocess.run(["git", "worktree", "prune"], cwd=root, timeout=15)
        wt = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=15).stdout
        for line in wt.splitlines():
            if line.startswith("worktree ") and "zax-selfupdate-" in line:
                path = line.split(" ", 1)[1]
                subprocess.run(["git", "worktree", "remove", "--force", path], cwd=root, timeout=15)
        branches = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)",
                                   "refs/heads/self-update"], cwd=root,
                                  capture_output=True, text=True, timeout=15).stdout.split()
        # Keep only branches referenced by a still-pending approval; drop the rest.
        keep = set()
        for a in db.pending_approvals():
            if a["tool"] == "self_update":
                try:
                    keep.add(json.loads(a.get("meta") or "{}").get("branch", ""))
                except ValueError:
                    pass
        for b in branches:
            if b and b not in keep:
                subprocess.run(["git", "branch", "-D", b], cwd=root, timeout=15)
    except Exception as exc:
        db.log_event("error", "selfupdate", f"recover() failed: {str(exc)[:120]}")


async def propose_and_test(goal: str, requester: str = "zax",
                           repo_root: Path | None = None) -> dict:
    """Propose a self-code-change for `goal` in an isolated worktree; test it there;
    raise an approval request iff it passes. Returns a result dict — never raises.
    The worktree is ALWAYS removed (finally); the branch survives only on success."""
    if not config.ALLOW_SELF_UPDATE:
        return {"ok": False, "error": "self-update is disabled (ZAX_ALLOW_SELF_UPDATE=0)"}
    if _ONE_UPDATE.locked():
        return {"ok": False, "busy": True, "current_goal": _PROGRESS.get("goal", ""),
                "error": "another self-update is already in progress"}
    async with _ONE_UPDATE:
        import tempfile
        root = repo_root or config.ROOT
        branch = f"self-update/{uuid.uuid4().hex[:8]}"
        worktree = Path(tempfile.mkdtemp(prefix="zax-selfupdate-"))
        _PROGRESS.update({"active": True, "goal": goal, "phase": "starting", "started": time.time()})
        keep_branch = False
        try:
            rc, out = await _git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=root)
            if rc != 0:
                return {"ok": False, "error": f"could not create worktree: {out[:300]}"}
            rc, _ = await _git("checkout", "-b", branch, cwd=worktree)
            if rc != 0:
                return {"ok": False, "error": "could not create branch"}

            _PROGRESS["phase"] = "editing the code"
            summary = await _run_edit_loop(goal, worktree)

            rc, diffstat = await _git("diff", "--stat", "HEAD", cwd=worktree)
            if not diffstat.strip():
                db.log_event("selfupdate", requester,
                             f"Self-update for “{goal[:50]}” made no change — nothing to ship")
                return {"ok": False, "error": "no code change was produced for that goal",
                        "summary": summary}

            _PROGRESS["phase"] = "running the test suite"
            db.log_event("selfupdate", requester,
                         f"Proposed change for “{goal[:50]}” — running the test suite…")
            test_rc, test_out = await _run_tests(worktree)
            if test_rc != 0:
                db.log_event("selfupdate", requester,
                             f"Self-update for “{goal[:50]}” failed its own tests — discarded")
                return {"ok": False, "error": "tests failed after the change",
                        "test_output": test_out[-3000:]}

            await _git("add", "-A", "--", "zax", "tests", cwd=worktree)
            rc, out = await _git("commit", "-m", f"Self-update: {goal[:72]}\n\n{summary}", cwd=worktree)
            if rc != 0:
                return {"ok": False, "error": f"commit failed: {out[:300]}"}

            _, full_diff = await _git("diff", "HEAD~1", "--", "zax", "tests", cwd=worktree)
            aid = db.add_approval(
                agent=requester, task_id="", task_title=goal[:300], tool="self_update",
                command=(diffstat + "\n\n" + full_diff)[:6000],
                reason="self-authored code change — full test suite passed",
                meta=json.dumps({"branch": branch, "goal": goal}),
            )
            keep_branch = True  # preserved until the Founder approves or denies it
            db.log_event("selfupdate", requester,
                         f"Self-update for “{goal[:50]}” passed tests — awaiting Founder approval (#{aid})")
            return {"ok": True, "approval_id": aid, "branch": branch, "summary": summary}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}
        finally:
            await _git("worktree", "remove", "--force", str(worktree), cwd=root)
            if not keep_branch:
                await _git("branch", "-D", branch, cwd=root)
            _PROGRESS.update({"active": False, "phase": "idle"})


async def _run_edit_loop(goal: str, worktree: Path) -> str:
    system = _SYSTEM.format(goal=goal, tools=_TOOL_SPECS)
    messages = [{"role": "user", "content": "Make the change now."}]
    for step in range(config.SELF_UPDATE_MAX_STEPS):
        text, _ = await llm.chat(system, messages, max_tokens=4000)
        parsed = llm.extract_json(text)
        if parsed and parsed.get("tool"):
            name, args = str(parsed["tool"]), parsed.get("args") or {}
            if name == "list_source":
                out = _list_source(worktree, str(args.get("path", "")))
            elif name == "read_source":
                out = _read_source(worktree, str(args.get("path", "")))
            elif name == "write_source":
                out = _write_source(worktree, str(args.get("path", "")), str(args.get("content", "")))
            elif name == "delete_source":
                out = _delete_source(worktree, str(args.get("path", "")))
            else:
                out = f"Unknown tool: {name}"
            steps_left = config.SELF_UPDATE_MAX_STEPS - step - 1
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"RESULT:\n{out[:6000]}\n\n"
                             f"({steps_left} steps left. Read before you write. "
                             'Finish with {"final": "..."} when done.)'})
            continue
        if parsed and parsed.get("final") is not None:
            return str(parsed["final"])
        return text.strip()[:800]  # model stopped without a clean final — use what it said
    return "(hit the step limit before signalling completion)"


async def _run_tests(worktree: Path) -> tuple[int, str]:
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-q",
        cwd=str(worktree),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=config.SELF_UPDATE_TEST_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"test suite timed out after {config.SELF_UPDATE_TEST_TIMEOUT}s"
    return proc.returncode, out.decode(errors="replace")


async def _delayed_restart(delay: float = 1.5) -> None:
    await asyncio.sleep(delay)
    os._exit(0)  # launchd (KeepAlive) / __main__'s retry loop relaunches with the new code


async def apply_approved(approval: dict, repo_root: Path | None = None) -> str:
    """Merge an approved self-update into main and restart. Never raises — returns a
    human-readable outcome string that becomes the approval's stored output."""
    root = repo_root or config.ROOT
    try:
        meta = json.loads(approval.get("meta") or "{}")
    except ValueError:
        meta = {}
    branch = meta.get("branch", "")
    if not branch:
        return "No branch recorded for this approval — nothing to merge."

    rc, cur_branch = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if rc != 0 or cur_branch.strip() != "main":
        return f"Refused: main repo isn't on 'main' (on '{cur_branch.strip()}') — merge manually."

    _, pre_hash = await _git("rev-parse", "HEAD", cwd=root)
    pre_hash = pre_hash.strip()[:12]

    rc, out = await _git("merge", "--no-ff", branch, "-m",
                         f"Merge self-update: {meta.get('goal', '')[:72]}", cwd=root)
    if rc != 0:
        await _git("merge", "--abort", cwd=root)
        return (f"Merge conflict — not applied, nothing restarted. The branch '{branch}' is "
                f"preserved; resolve manually with `git merge {branch}`.\n{out[:500]}")

    await _git("branch", "-d", branch, cwd=root)
    db.log_event("selfupdate", "founder", f"Self-update merged ({pre_hash} -> new) — restarting")
    asyncio.create_task(_delayed_restart())
    return (f"Merged and restarting now (a few seconds of downtime). "
            f"Rollback if anything looks wrong: git reset --hard {pre_hash}")
