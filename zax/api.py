"""HTTP API — thin glue over ceo/db/voice. The UI in static/ is the only client."""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import ceo, config, db, graph, learning, llm, pipeline, skills, telegram, tools, voice

router = APIRouter(prefix="/api")


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str = Field(default="main", max_length=40)


class SessionRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8000)
    priority: int = Field(default=2, ge=1, le=3)


class HireIn(BaseModel):
    role: str = Field(min_length=2, max_length=300)


class FireIn(BaseModel):
    reason: str = Field(default="Founder's directive", max_length=500)


class RoutineIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    interval_minutes: int = Field(ge=5, le=10080)


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ProviderSelectIn(BaseModel):
    provider: str = Field(min_length=1, max_length=40)


class ProviderConfigIn(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)


@router.get("/status")
async def status():
    state = ceo.org_state()
    state["voice_server_tts"] = voice.available()
    state["heartbeat_seconds"] = config.HEARTBEAT_SECONDS
    state["circuit_breaker"] = pipeline.status()
    state["provider_online"] = llm.is_configured(llm.resolve_provider())
    # Show the effective provider after auto-fallback (may differ from configured)
    state["_effective_provider"] = llm.effective_provider()
    return state


@router.get("/greeting")
async def greeting():
    text = await ceo.greeting()
    db.log_event("boot", "zax", "Zax greeted the Founder")
    return {"text": text, "founder": config.FOUNDER_NAME}


@router.post("/chat")
async def chat(body: ChatIn):
    return await ceo.chat(body.message, body.session_id)


@router.get("/messages")
async def messages(session: str = "main"):
    return db.recent_messages(120, session)


# ------------------------------------------------------------------ chat sessions

@router.get("/sessions")
async def sessions():
    return db.list_sessions()


@router.post("/sessions")
async def new_session():
    return db.create_session()


@router.post("/sessions/{session_id}/rename")
async def rename_session(session_id: str, body: SessionRenameIn):
    if not db.get_session(session_id):
        raise HTTPException(404, "No such session")
    db.rename_session(session_id, body.title)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    if session_id == "main":
        raise HTTPException(400, "The main chat can't be deleted")
    db.delete_session(session_id)
    return {"ok": True}


@router.get("/feed")
async def feed(after: int = 0):
    return db.events_since(after, limit=60)


# ------------------------------------------------------------------ providers

@router.get("/providers")
async def providers():
    out = llm.provider_overview()
    # Ollama's catalogue is whatever is pulled locally — ask it live (fast timeout) so
    # the core dropdown offers the real tags (e.g. gemma4:12b-mlx), not a stale default
    # that 404s when selected.
    try:
        import httpx
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.get(f"{llm.base_url('ollama')}/api/tags")
            tags = [m["name"] for m in r.json().get("models", [])]
        if tags:
            for p in out:
                if p["id"] == "ollama":
                    p["models"] = tags
    except Exception:
        pass  # ollama not running — keep the static entry
    return out


class ProjectIn(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)


@router.get("/projects")
async def list_projects():
    from . import project
    return project.overview()


@router.post("/projects")
async def start_project(body: ProjectIn):
    # Plan synchronously here (the API caller can wait for the plan), then kick the org.
    from . import project as project_mod
    proj = await project_mod.plan_and_start(body.goal)
    if proj is None:
        t = db.create_task(body.goal[:300], body.goal, priority=1)
        pipeline.kick("single task from project endpoint")
        return {"ok": True, "single_task": t["id"], "note": "goal was a single task, not a project"}
    pipeline.kick("project planned")
    return {"ok": True, "project_id": proj["id"], "steps": len(db.project_tasks(proj["id"]))}


class SelfUpdateIn(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)


@router.get("/self-update/status")
async def self_update_status():
    from . import selfupdate
    return selfupdate.status()


@router.post("/self-update")
async def trigger_self_update(body: SelfUpdateIn):
    """Kick off a self-update from the Bridge button. Fires in the background (propose
    + full test suite takes ~30-90s) and reports via events/approvals. Returns busy=True
    (not a false 'on it') if one is already running, so the UI can say so honestly."""
    if not config.ALLOW_SELF_UPDATE:
        raise HTTPException(400, "Self-update is disabled (ZAX_ALLOW_SELF_UPDATE=0)")
    from . import selfupdate
    st = selfupdate.status()
    if st["active"]:
        return {"ok": False, "busy": True, "current_goal": st["goal"], "phase": st["phase"]}
    # Mark active synchronously so a rapid second click sees 'busy' too, then fire.
    asyncio.create_task(ceo._self_update_bg(body.goal))
    db.log_event("selfupdate", "founder", f"The Founder requested a self-update: {body.goal[:100]}")
    return {"ok": True, "note": "Writing the change now — it'll land in the approval bar if it passes tests."}


class CoreSetIn(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(default="", max_length=120)


@router.post("/core")
async def set_core(body: CoreSetIn):
    """One-call core switch for the chat dropdown: provider + optional model."""
    pid = body.provider.strip().lower()
    if pid not in llm.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    if not llm.is_configured(pid):
        raise HTTPException(400, f"{llm.PROVIDERS[pid]['label']} has no API key configured")
    eff = llm.set_core(pid, body.model)
    # A fresh core deserves a clean slate: the breaker/error banner belonged to the
    # OLD core — clearing it stops "⚠ CORE ERROR" lingering after a switch — and the
    # kick retries any stalled work on the new brain immediately.
    pipeline._clear_backoff()
    pipeline.kick("core switched")
    db.log_event("config", "founder",
                 f"The Founder switched the core to {llm.PROVIDERS[pid]['label']} · {eff}")
    return {"ok": True, "provider": pid, "model": eff}


@router.post("/providers/select")
async def select_provider(body: ProviderSelectIn):
    if body.provider not in llm.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    db.set_setting("provider.active", body.provider)
    db.log_event("config", "founder",
                 f"The Founder switched the intelligence core to {llm.PROVIDERS[body.provider]['label']}")
    return {"ok": True, "active": llm.resolve_provider()}


@router.post("/providers/configure")
async def configure_provider(body: ProviderConfigIn):
    if body.provider not in llm.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    if body.api_key is not None:
        db.set_setting(f"provider.{body.provider}.api_key", body.api_key.strip())
    if body.model is not None:
        db.set_setting(f"provider.{body.provider}.model", body.model.strip())
    if body.base_url is not None and body.provider == "custom":
        db.set_setting("provider.custom.base_url", body.base_url.strip())
    db.log_event("config", "founder", f"The Founder updated {body.provider} configuration")
    return {"ok": True, "configured": llm.is_configured(body.provider)}


class ApprovalIn(BaseModel):
    decision: str = Field(pattern="^(approve|deny)$")


@router.get("/approvals")
async def list_approvals():
    return db.pending_approvals()


@router.post("/approvals/{aid}")
async def resolve_approval(aid: int, body: ApprovalIn):
    a = db.get_approval(aid)
    if not a or a["status"] != "pending":
        raise HTTPException(404, "No such pending approval")
    if body.decision == "deny":
        db.resolve_approval(aid, "denied")
        if a["tool"] == "self_update":
            import json as _json
            from . import selfupdate
            branch = _json.loads(a.get("meta") or "{}").get("branch", "")
            if branch:
                await selfupdate._git("branch", "-D", branch, cwd=config.ROOT)
        db.log_event("approval", "founder", f"The Founder DENIED {a['tool']}: {a['command'][:100]}")
        return {"ok": True, "status": "denied"}
    # Approve → actually run the held command now, capture output.
    if a["tool"] == "self_update":
        from . import selfupdate
        out = await selfupdate.apply_approved(a)
    else:
        out = await tools.run_approved(a["tool"], a["command"])
    db.resolve_approval(aid, "approved", out)
    db.log_event("approval", "founder",
                 f"The Founder APPROVED and ran {a['tool']}: {a['command'][:100]}")
    return {"ok": True, "status": "approved", "output": out[:6000]}


@router.post("/tools/tavily")
async def configure_tavily(request: Request):
    """Set (or clear) the Tavily search key. Accepts JSON regardless of content-type so
    a browser can post it cross-origin as a CORS 'simple request' (no preflight) — the
    key travels page -> localhost -> settings DB without transiting any third party."""
    import json as _json
    try:
        body = _json.loads((await request.body()) or b"{}")
    except ValueError:
        raise HTTPException(400, "Body must be JSON")
    key = str(body.get("api_key", "")).strip()
    if key and not key.startswith("tvly-"):
        raise HTTPException(400, "Not a Tavily key (must start with tvly-)")
    db.set_setting("tools.tavily_api_key", key)
    db.log_event("config", "founder",
                 "The Founder " + ("configured" if key else "cleared") + " Tavily web search")
    return {"ok": True, "tavily": bool(key)}


@router.post("/providers/test")
async def test_provider(body: ProviderSelectIn):
    if body.provider not in llm.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    started = time.monotonic()
    try:
        text, tokens = await llm.chat(
            "You are a connectivity probe. Reply with one short sentence.",
            [{"role": "user", "content": "Confirm you are online."}],
            max_tokens=50,
            provider=body.provider,
        )
        return {"ok": True, "reply": text.strip()[:200], "tokens": tokens,
                "seconds": round(time.monotonic() - started, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:400],
                "seconds": round(time.monotonic() - started, 1)}


# ------------------------------------------------------------------ org

@router.get("/agents")
async def agents():
    return db.all_agents()


@router.get("/skills")
async def skill_catalog():
    """The specialist skill packs Zax can hire from, grouped by category."""
    on_staff = {a["skill"] for a in db.active_agents() if a["skill"]}
    cats = {}
    for cat, packs in skills.by_category().items():
        cats[cat] = [
            {"key": p["key"], "name": p["name"], "title": p["title"],
             "emoji": p["emoji"], "role": p["role"], "hired": p["key"] in on_staff}
            for p in packs
        ]
    return cats


class SkillHireIn(BaseModel):
    skill: str = Field(min_length=1, max_length=40)


@router.post("/skills/hire")
async def hire_skill(body: SkillHireIn):
    if not skills.get(body.skill):
        raise HTTPException(404, "Unknown skill")
    agent = ceo.hire_from_skill(body.skill)
    db.log_event("hire", "founder", f"The Founder hired {agent['name']} — {agent['title']}")
    pipeline.kick("specialist hire")
    return agent


@router.post("/hire")
async def hire(body: HireIn):
    agent = await ceo.hire_with_llm(body.role)
    pipeline.kick("new hire")  # let the new agent pick up backlog now
    return agent


@router.post("/agents/{agent_id}/fire")
async def fire(agent_id: str, body: FireIn):
    agent = db.get_agent(agent_id)
    if not agent or agent["status"] == "fired":
        raise HTTPException(404, "No such active agent")
    db.fire_agent(agent_id, body.reason)
    db.log_event("fire", "founder", f"The Founder terminated {agent['name']} — {body.reason}")
    pipeline.kick("post-firing redistribution")  # reassign their tickets now
    return {"ok": True}


# ------------------------------------------------------------------ tasks

@router.get("/tasks")
async def tasks():
    return db.all_tasks()


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "No such task")
    if t.get("agent_id"):
        agent = db.get_agent(t["agent_id"])
        t["agent_name"] = agent["name"] if agent else None
    return t


@router.post("/tasks")
async def create_task(body: TaskIn):
    task = db.create_task(body.title, body.description, body.priority)
    db.log_event("task", "founder", f"The Founder filed task: {task['title']}")
    pipeline.kick("filed task")  # execute immediately, not on the next heartbeat
    return task


@router.post("/run")
async def run_now():
    """Force the org to assign + execute + review everything pending, right now."""
    return await pipeline.run_now()


# ------------------------------------------------------------------ routines

@router.get("/routines")
async def routines():
    return db.all_routines()


@router.post("/routines")
async def create_routine(body: RoutineIn):
    return db.create_routine(body.name, body.description, body.interval_minutes)


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: str):
    db.delete_routine(routine_id)
    return {"ok": True}


# ------------------------------------------------------------------ memory & learning

@router.get("/memory")
async def memory(q: str = "", kind: str = ""):
    if q:
        return db.recall(q, limit=20, kinds=[kind] if kind else None)
    return db.list_memories(kind=kind)


@router.delete("/memory/{mem_id}")
async def delete_memory(mem_id: int):
    db.delete_memory(mem_id)
    return {"ok": True}


@router.get("/learning/status")
async def learning_status():
    return learning.status()


# ------------------------------------------------------------------ knowledge graph

@router.get("/graph")
async def graph_data():
    return graph.graph_json()


@router.get("/graph/stats")
async def graph_stats():
    return graph.stats()


@router.get("/graph/query")
async def graph_query(q: str = ""):
    if not q.strip():
        raise HTTPException(400, "missing query")
    return graph.query(q)


@router.get("/graph/path")
async def graph_path(from_: str = "", to: str = ""):
    if not from_.strip() or not to.strip():
        raise HTTPException(400, "need 'from_' and 'to'")
    return graph.path(from_, to)


@router.get("/graph/explain")
async def graph_explain(node: str = ""):
    if not node.strip():
        raise HTTPException(400, "missing node")
    return graph.explain(node)


@router.delete("/graph/node/{node_id}")
async def graph_delete_node(node_id: str):
    db.delete_node(node_id)
    return {"ok": True}


@router.post("/graph/rebuild")
async def graph_rebuild():
    result = await graph.rebuild()
    db.log_event("graph", "founder", "The Founder rebuilt the memory graph")
    return result


@router.post("/learning/reflect")
async def reflect_now():
    result = await learning.daily_reflection(force=True)
    return result


# ------------------------------------------------------------------ telegram

class TelegramConnectIn(BaseModel):
    token: str = Field(min_length=20, max_length=120)


class TelegramNotifyIn(BaseModel):
    enabled: bool


@router.get("/telegram")
async def telegram_status():
    return telegram.status()


@router.post("/telegram/connect")
async def telegram_connect(body: TelegramConnectIn):
    try:
        username = await telegram.validate(body.token.strip())
    except Exception as exc:
        raise HTTPException(400, f"Invalid bot token: {exc}")
    await telegram.stop()
    db.set_setting("telegram.token", body.token.strip())
    db.set_setting("telegram.username", username)
    db.set_setting("telegram.chat_id", "")  # re-link via /start on the new bot
    telegram.start()
    db.log_event("config", "founder", f"The Founder connected Telegram bot @{username}")
    return telegram.status()


@router.post("/telegram/disconnect")
async def telegram_disconnect():
    await telegram.stop()
    for k in ("telegram.token", "telegram.username", "telegram.chat_id"):
        db.set_setting(k, "")
    return telegram.status()


@router.post("/telegram/notify")
async def telegram_notify(body: TelegramNotifyIn):
    db.set_setting("telegram.notify", "1" if body.enabled else "0")
    return telegram.status()


# ------------------------------------------------------------------ voice

class VoiceConfigIn(BaseModel):
    provider: str | None = Field(default=None, max_length=20)
    edge_voice: str | None = Field(default=None, max_length=60)
    eleven_voice: str | None = Field(default=None, max_length=60)
    eleven_key: str | None = Field(default=None, max_length=200)


@router.post("/voice/speak")
async def speak(body: SpeakIn):
    if not voice.available():
        raise HTTPException(503, "Server TTS unavailable — client should fall back to Web Speech API")
    try:
        audio, mime = await voice.synthesize(body.text)
    except Exception as exc:
        raise HTTPException(503, f"TTS failed: {exc}")
    return Response(content=audio, media_type=mime)


@router.get("/voice/config")
async def voice_config():
    return voice.public_config()


@router.post("/voice/config")
async def set_voice_config(body: VoiceConfigIn):
    if body.eleven_key is not None and body.eleven_key.strip():
        try:
            ok = await voice.verify_eleven(body.eleven_key.strip())
        except Exception:
            ok = False
        if not ok:
            raise HTTPException(400, "ElevenLabs key rejected")
        db.set_setting("voice.eleven_key", body.eleven_key.strip())
    if body.provider is not None:
        db.set_setting("voice.provider", body.provider)
    if body.edge_voice is not None:
        db.set_setting("voice.edge_voice", body.edge_voice)
    if body.eleven_voice is not None:
        db.set_setting("voice.eleven_voice", body.eleven_voice)
    db.log_event("config", "founder", "The Founder updated Zax's voice")
    return voice.public_config()
