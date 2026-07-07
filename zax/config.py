"""Zax configuration. Everything is overridable via environment variables / .env."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ZAX_DATA_DIR", str(ROOT / "data")))
WORKSPACE_DIR = DATA_DIR / "workspace"
DB_PATH = DATA_DIR / "zax.db"
# When set (e.g. a Supabase Postgres connection string), Zax stores everything in
# Postgres instead of the local SQLite file. Blank = SQLite (local + tests).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

FOUNDER_NAME = os.environ.get("ZAX_FOUNDER_NAME", "Bhupen")
HOST = os.environ.get("ZAX_HOST", "127.0.0.1")
# Cloud hosts (Railway/Render/Fly) inject $PORT; honour it, else ZAX_PORT, else 8777.
PORT = int(os.environ.get("PORT") or os.environ.get("ZAX_PORT") or "8777")
# When set, the whole app is gated behind HTTP Basic auth (any username + this
# password). REQUIRED for a public deploy so strangers can't drive your org.
ACCESS_PASSWORD = os.environ.get("ZAX_ACCESS_PASSWORD", "")

# LLM provider. "auto" prefers the Claude subscription (terminal login) when the
# `claude` CLI is installed, then falls through to any configured API key.
# Full registry + runtime configuration live in llm.py / the Settings panel.
PROVIDER = os.environ.get("ZAX_PROVIDER", "auto")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
ZAX_MODEL = os.environ.get("ZAX_MODEL", "")  # blank = provider default

# Scheduling / org policy. Zax is event-driven — tasks run the instant they're
# delegated (pipeline.kick) and run to completion. SCHEDULER_SECONDS is ONLY how
# often the routine scheduler wakes to fire due recurring routines + recover
# restart-stranded work; it is not a task-execution poll.
SCHEDULER_SECONDS = int(os.environ.get("ZAX_SCHEDULER_SECONDS",
                        os.environ.get("ZAX_HEARTBEAT_SECONDS", "60")))
HEARTBEAT_SECONDS = SCHEDULER_SECONDS  # back-compat alias for /api/status
MAX_TOOL_STEPS = int(os.environ.get("ZAX_MAX_TOOL_STEPS", "10"))
# Claude-style autonomous loop: every deliverable is adversarially self-checked (and
# revised once if issues are found) BEFORE it reaches review. The checker runs on the
# core named by the `verify.core` setting ("provider/model", e.g. "ollama/ornith:9b"
# for a free local critic) and falls back to the active core.
SELF_CHECK = os.environ.get("ZAX_SELF_CHECK", "1") == "1"
# The verify loop revises & re-checks until the critic passes, up to this many rounds.
MAX_VERIFY_ROUNDS = int(os.environ.get("ZAX_MAX_VERIFY_ROUNDS", "3"))
MAX_EXECUTIONS_PER_TICK = int(os.environ.get("ZAX_MAX_EXECUTIONS_PER_TICK", "2"))
# When you delegate work, Zax drains the queue immediately (not on the next
# heartbeat). This caps how many tasks one immediate drain executes before
# yielding, so a huge backlog can't run away in a single burst.
MAX_DRAIN_EXECUTIONS = int(os.environ.get("ZAX_MAX_DRAIN_EXECUTIONS", "25"))
MAX_HEADCOUNT = int(os.environ.get("ZAX_MAX_HEADCOUNT", "8"))
FIRE_THRESHOLD = float(os.environ.get("ZAX_FIRE_THRESHOLD", "50"))
MIN_TASKS_BEFORE_FIRE = int(os.environ.get("ZAX_MIN_TASKS_BEFORE_FIRE", "3"))
HIRE_BACKLOG_PER_AGENT = int(os.environ.get("ZAX_HIRE_BACKLOG_PER_AGENT", "3"))
DEFAULT_TOKEN_BUDGET = int(os.environ.get("ZAX_DEFAULT_TOKEN_BUDGET", "250000"))

# Agent tools. This is what makes agents OPERATORS, not just
# writers: run_code lets them execute and VERIFY their work before delivering.
# It runs in an isolated Python subprocess (`-I`), workspace cwd, restricted env,
# hard timeout — process isolation, not a security sandbox, but scoped enough to be
# on by default. SHELL is arbitrary (`rm -rf` has no allowlist) so it stays opt-in.
ALLOW_CODE = os.environ.get("ZAX_ALLOW_CODE", "1") == "1"
# Let agents hand whole tasks to external agent apps installed on this machine
# (Hermes, opencode) and use their result — "control my projects with other agents".
ALLOW_EXTERNAL_AGENTS = os.environ.get("ZAX_ALLOW_EXTERNAL_AGENTS", "1") == "1"
# Shell is ON, but a command approval gate (tools.py) holds anything not on a tight
# read-only allowlist for the Founder to Approve/Deny — so `ls` runs but `rm -rf`
# waits. Set ZAX_ALLOW_SHELL=0 to disable shell entirely.
ALLOW_SHELL = os.environ.get("ZAX_ALLOW_SHELL", "1") == "1"
CODE_TIMEOUT = int(os.environ.get("ZAX_CODE_TIMEOUT", "30"))

# Zax voice. Engine + voice are chosen at runtime in Settings (stored in SQLite);
# these are only fallbacks. Premium ElevenLabs key can also come from the env.
TTS_VOICE = os.environ.get("ZAX_TTS_VOICE", "en-GB-RyanNeural")
TTS_PITCH = os.environ.get("ZAX_TTS_PITCH", "-2Hz")
TTS_RATE = os.environ.get("ZAX_TTS_RATE", "+0%")
ELEVEN_KEY_ENV = os.environ.get("ELEVENLABS_API_KEY", "")


