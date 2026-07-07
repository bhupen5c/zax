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
    parts = Path(rel).parts if rel else ()
    if not parts or ".." in parts or Path(rel).is_absolute():
        raise ValueError("invalid path")
    if parts[0] not in ALLOWED_PREFIXES or ".git" in parts:
        raise ValueError(f"path must be under {'/'.join(ALLOWED_PREFIXES)}/ — refused: {rel}")
    p = (root / rel).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("path escapes the repo")
    return p


def _list_source(root: Path, rel: str) -> str:
    # Listing the repo root just shows the editable trees (avoids resolving an empty path).
    if (rel or "").strip().strip("/.") == "":
        return "\n".join(f"{p}/" for p in ALLOWED_PREFIXES)
    try:
        base = _safe_source_path(root, rel)
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

_PLAN_SYSTEM = """You are Zax's self-improvement architect. The Founder gave a goal for
changing Zax's OWN source code. Break it into an ORDERED list of concrete, individually
implementable steps — each one a self-contained change a single engineer could make and
test on its own. A small, single change is just ONE step. A big "vision" goal becomes
several focused steps (typically 2-6). Keep steps minimal and independent where possible;
put foundational changes (config/schema/helpers) before the code that uses them.

Only zax/ and tests/ are editable. Never propose weakening safety (approval gate,
path restrictions, encryption, the verify loop). If part of the goal is vague or risky,
turn it into the safest concrete interpretation.

Reply with ONLY: {"steps": [{"title": "short label", "detail": "precise instruction for this one change"}]}"""


_ONE_UPDATE = asyncio.Lock()


async def _plan(goal: str) -> list[dict]:
    """Decompose the goal into ordered concrete steps. Falls back to a single step
    (the goal itself) if planning fails or returns nothing usable."""
    try:
        text, _ = await llm.chat(_PLAN_SYSTEM, [{"role": "user", "content": f"GOAL: {goal}"}],
                                 max_tokens=1500)
        parsed = llm.extract_json(text) or {}
        steps = [s for s in (parsed.get("steps") or [])
                 if isinstance(s, dict) and str(s.get("detail", "")).strip()]
        if steps:
            return steps[:config.SELF_UPDATE_MAX_PLAN_STEPS]
    except Exception:
        pass
    return [{"title": goal[:60], "detail": goal}]


async def _test_with_fixes(detail: str, worktree: Path, progress_label: str = "") -> tuple[int, str]:
    """Run the test suite; while it fails, let the model read the failure and fix it,
    up to SELF_UPDATE_FIX_ATTEMPTS times. Returns the final (rc, output)."""
    for attempt in range(config.SELF_UPDATE_FIX_ATTEMPTS + 1):
        _PROGRESS["phase"] = (f"{progress_label}: testing" if progress_label else "running the test suite")
        rc, out = await _run_tests(worktree)
        if rc == 0 or attempt == config.SELF_UPDATE_FIX_ATTEMPTS:
            return rc, out
        _PROGRESS["phase"] = (f"{progress_label}: fixing test failure" if progress_label
                              else "fixing a test failure")
        fix_goal = (f"Your change for “{detail[:120]}” broke the test suite. Read the failing "
                    f"tests and the code, and FIX it so the whole suite passes again. Do not "
                    f"delete or weaken tests to make them pass.\n\nTEST OUTPUT:\n{out[-2500:]}")
        await _run_edit_loop(fix_goal, worktree)
    return rc, out


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
            if line.startswith("worktree ") and ("selfupdate-worktrees" in line
                                                  or "zax-selfupdate-" in line):
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
        root = repo_root or config.ROOT
        slug = uuid.uuid4().hex[:8]
        branch = f"self-update/{slug}"
        # Keep the worktree in the app's own data dir (stable, git-ignored) — NOT system
        # temp, which can be reaped under launchd mid-run and vanish out from under us.
        wt_base = config.DATA_DIR / "selfupdate-worktrees"
        wt_base.mkdir(parents=True, exist_ok=True)
        worktree = wt_base / slug
        _PROGRESS.update({"active": True, "goal": goal, "phase": "starting", "started": time.time()})
        keep_branch = False
        try:
            rc, out = await _git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=root)
            if rc != 0:
                return {"ok": False, "error": f"could not create worktree: {out[:300]}"}
            rc, _ = await _git("checkout", "-b", branch, cwd=worktree)
            if rc != 0:
                return {"ok": False, "error": "could not create branch"}

            _, base = await _git("rev-parse", "HEAD", cwd=worktree)
            base = base.strip()

            # Decompose the goal. A small change -> a 1-step plan (fast path); a big
            # vision -> several concrete, individually-testable steps.
            _PROGRESS["phase"] = "planning the change"
            steps = await _plan(goal)
            multi = len(steps) > 1
            db.log_event("selfupdate", requester,
                         f"Self-update for “{goal[:50]}” — planned {len(steps)} step(s)")

            landed: list[str] = []
            skipped: list[str] = []
            for i, step in enumerate(steps, 1):
                title = str(step.get("title", f"step {i}"))[:80]
                detail = str(step.get("detail", title))
                _PROGRESS["phase"] = (f"step {i}/{len(steps)}: {title}" if multi else "editing the code")
                await _run_edit_loop(detail, worktree)

                # Test this step; give the model a couple of chances to fix a failure
                # before we give up on it.
                test_rc, test_out = await _test_with_fixes(detail, worktree,
                                                           progress_label=f"step {i}/{len(steps)}" if multi else "")
                if test_rc == 0 and (await _git("status", "--porcelain", cwd=worktree))[1].strip():
                    await _git("add", "-A", "--", "zax", "tests", cwd=worktree)
                    await _git("commit", "-m", f"{title}", cwd=worktree)
                    landed.append(title)
                else:
                    # Revert just this step's edits back to the last green checkpoint,
                    # then carry on with the rest of the plan.
                    await _git("reset", "--hard", cwd=worktree)
                    await _git("clean", "-fd", "--", "zax", "tests", cwd=worktree)
                    if test_rc != 0:
                        skipped.append(title)

            rc, cum_diffstat = await _git("diff", "--stat", base, cwd=worktree)
            if not cum_diffstat.strip():
                db.log_event("selfupdate", requester,
                             f"Self-update for “{goal[:50]}” produced no shippable change")
                return {"ok": False, "error": "no code change survived (nothing passed its tests)",
                        "skipped": skipped}

            _, full_diff = await _git("diff", base, "--", "zax", "tests", cwd=worktree)
            plan_note = ""
            if multi:
                plan_note = ("Plan: " + "; ".join(landed)
                             + (f"  |  skipped (failed tests): {', '.join(skipped)}" if skipped else "")
                             + "\n\n")
            aid = db.add_approval(
                agent=requester, task_id="", task_title=goal[:300], tool="self_update",
                command=(plan_note + cum_diffstat + "\n\n" + full_diff)[:8000],
                reason=(f"self-authored change — {len(landed)} step(s) landed, full test suite passes"),
                meta=json.dumps({"branch": branch, "goal": goal}),
            )
            keep_branch = True  # preserved until the Founder approves or denies it
            db.log_event("selfupdate", requester,
                         f"Self-update for “{goal[:50]}” — {len(landed)} step(s) passed tests, "
                         f"awaiting Founder approval (#{aid})")
            return {"ok": True, "approval_id": aid, "branch": branch,
                    "landed": landed, "skipped": skipped}
        except Exception as exc:
            import traceback
            db.log_event("error", "selfupdate",
                         f"propose_and_test crashed: {traceback.format_exc()[-600:]}")
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:250]}"}
        finally:
            await _git("worktree", "remove", "--force", str(worktree), cwd=root)
            if not keep_branch:
                await _git("branch", "-D", branch, cwd=root)
            _PROGRESS.update({"active": False, "phase": "idle"})


async def _run_edit_loop(goal: str, worktree: Path) -> str:
    system = _SYSTEM.format(goal=goal, tools=_TOOL_SPECS)
    messages = [{"role": "user", "content": "Make the change now."}]
    for step in range(config.SELF_UPDATE_MAX_STEPS):
        # 8000, not 4000: reasoning models (GLM-5.2, DeepSeek v4-pro, Kimi) spend hidden
        # thinking tokens from this budget and a whole file's content can be large.
        text, _ = await llm.chat(system, messages, max_tokens=8000)
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
