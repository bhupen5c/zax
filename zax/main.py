"""Zax — AI CEO & agent orchestration. FastAPI app entrypoint."""
import base64
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import api, ceo, config, db, heartbeat, llm, skills, telegram

STATIC = Path(__file__).parent / "static"

BANNER = r"""
  ███████╗ █████╗ ██╗  ██╗
  ╚══███╔╝██╔══██╗╚██╗██╔╝
    ███╔╝ ███████║ ╚███╔╝
   ███╔╝  ██╔══██║ ██╔██╗
  ███████╗██║  ██║██╔╝ ██╗
  ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝   AI CEO · agent orchestration
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    ceo.ensure_org_seeded()
    # Agents hired before the provider registry existed carried a hard-coded model;
    # blank it so they follow the org-default model from Settings.
    db.execute("UPDATE agents SET model='' WHERE model IN "
               "('zax-mock-1','claude-sonnet-4-6','gpt-4o-mini','llama3.1')")
    # Recover tasks interrupted mid-execution by a previous shutdown: anything
    # left 'in_progress' without a result was killed before finishing — requeue
    # it so it runs again instead of being stranded.
    db.execute("UPDATE tasks SET status='inbox', agent_id=NULL, progress=0 "
               "WHERE status='in_progress' AND result IS NULL")
    # Finalize any task that was already scored but never moved to a terminal
    # status (legacy/partial-write artifacts) — otherwise the review query skips
    # it forever (it filters score IS NULL) and it shows as stuck 'in progress'.
    db.execute("UPDATE tasks SET status=CASE WHEN score < 30 THEN 'failed' ELSE 'done' END, "
               "progress=100 WHERE status IN ('assigned','in_progress') AND score IS NOT NULL")
    # Personas are snapshotted into the DB at hire time, which silently pins agents
    # to whatever skills.py said back then. Re-sync on every boot so skills.py stays
    # the single source of truth. Legacy agents that predate the skill column are
    # relinked by name (packs use fixed names like Quill/Cipher/Lyra).
    for pack in skills.SKILLS:
        db.execute("UPDATE agents SET skill=? WHERE skill IS NULL AND name=? AND status='active'",
                   (pack["key"], pack["name"]))
        db.execute("UPDATE agents SET skill=? WHERE skill='' AND name=? AND status='active'",
                   (pack["key"], pack["name"]))
        db.execute("UPDATE agents SET persona=? WHERE skill=? AND persona != ?",
                   (pack["persona"], pack["key"], pack["persona"]))
    from . import selfupdate
    selfupdate.recover()  # prune any self-update worktree/branch stranded by a crash/restart
    heartbeat.start()
    telegram.start()  # no-op unless a bot token is configured
    # A public bind with no password + code/shell enabled = anyone who finds the URL
    # owns your CEO. Refuse to run that combination unless explicitly overridden.
    public = config.HOST not in ("127.0.0.1", "localhost", "::1")
    if public and not config.ACCESS_PASSWORD:
        msg = ("Zax is bound publicly (ZAX_HOST=%s) with NO ZAX_ACCESS_PASSWORD. "
               "Anyone who finds this URL could command your CEO and run code. Set "
               "ZAX_ACCESS_PASSWORD, or ZAX_ALLOW_INSECURE=1 to override." % config.HOST)
        if os.environ.get("ZAX_ALLOW_INSECURE") != "1":
            raise RuntimeError(msg)
        print("\n⚠  " + msg + "\n")
        db.log_event("warn", "zax", msg)
    provider = llm.resolve_provider()
    print(BANNER)
    print(f"  Founder:   {config.FOUNDER_NAME}")
    print(f"  Provider:  {provider} ({llm.default_model(provider)})")
    print(f"  Bridge:    http://{config.HOST}:{config.PORT}\n")
    db.log_event("boot", "zax", "Zax core online")
    yield
    await heartbeat.stop()
    await telegram.stop()
    await llm._close_client()


app = FastAPI(title="Zax", description="AI CEO & agent orchestration", lifespan=lifespan)

# Local-API hardening. Browsers let any website fire "simple" cross-origin POSTs at
# 127.0.0.1; requiring JSON forces a CORS preflight (which fails — no CORS headers
# are served), and the Host allowlist defeats DNS-rebinding. Both only make sense
# while bound to loopback; binding elsewhere is an explicit LAN opt-in.
if config.HOST in ("127.0.0.1", "localhost", "::1"):
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost", "[::1]"])


@app.middleware("http")
async def require_json_posts(request: Request, call_next):
    if request.method == "POST" and not request.headers.get(
            "content-type", "").lower().startswith("application/json"):
        return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
    return await call_next(request)


@app.middleware("http")
async def access_password(request: Request, call_next):
    """Gate the whole app behind HTTP Basic when ZAX_ACCESS_PASSWORD is set —
    required for a public deploy. /healthz stays open for platform health checks."""
    if config.ACCESS_PASSWORD and request.url.path != "/healthz":
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                pw = base64.b64decode(hdr[6:]).decode("utf-8", "replace").split(":", 1)[1]
                ok = hmac.compare_digest(pw, config.ACCESS_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Zax"'})
    return await call_next(request)


app.include_router(api.router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC / "index.html"))


@app.get("/healthz")
async def healthz():
    return {"ok": True}
