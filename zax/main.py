"""Zax — AI CEO & agent orchestration. FastAPI app entrypoint."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import api, ceo, config, db, heartbeat, llm, telegram

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
    heartbeat.start()
    telegram.start()  # no-op unless a bot token is configured
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


app.include_router(api.router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC / "index.html"))
