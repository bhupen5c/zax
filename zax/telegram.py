"""Telegram integration — talk to Zax and get alerts from your phone.

Full control: two-way chat (every message runs through ceo.chat, so you can
delegate, ask for status, hire/fire just like the web Bridge), plus push
notifications for the org events you care about (task done, hire, fire, review).

The bot token is configured at runtime from Settings (stored in SQLite). The
first chat that sends /start is registered as the owner; the bot then ONLY
responds to that chat, so a stranger who finds the bot can't drive your org.

Two background loops run while connected:
  • _poll_updates — long-polls getUpdates and handles incoming messages.
  • _poll_events  — watches the event log and pushes notifications.
"""
import asyncio
import contextlib

import httpx

from . import db

API = "https://api.telegram.org/bot{token}/{method}"
SESSION = "telegram"  # Telegram chats live in their own conversation thread

_task: asyncio.Task | None = None


# ---------------------------------------------------------------- config

def token() -> str:
    return db.get_setting("telegram.token", "")


def chat_id() -> str:
    return db.get_setting("telegram.chat_id", "")


def notify_enabled() -> bool:
    return db.get_setting("telegram.notify", "1") == "1"


def configured() -> bool:
    return bool(token())


def status() -> dict:
    return {
        "connected": _task is not None and not _task.done(),
        "has_token": configured(),
        "chat_id": chat_id() or None,
        "username": db.get_setting("telegram.username", "") or None,
        "notify": notify_enabled(),
    }


# ---------------------------------------------------------------- low-level

async def _call(method: str, params: dict, timeout: float = 35) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(API.format(token=token(), method=method), json=params)
        r.raise_for_status()
        return r.json()


async def validate(tok: str) -> str:
    """Check a token via getMe; return the bot username or raise."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(API.format(token=tok, method="getMe"))
        data = r.json()
    if not data.get("ok"):
        raise ValueError(data.get("description", "invalid token"))
    return data["result"].get("username", "")


async def send(text: str, to: str = "") -> None:
    target = to or chat_id()
    if not target or not configured():
        return
    with contextlib.suppress(Exception):
        await _call("sendMessage", {"chat_id": target, "text": text[:4000],
                                    "disable_web_page_preview": True})


# ---------------------------------------------------------------- update loop

async def _handle_message(msg: dict) -> None:
    chat = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not text:
        return

    owner = chat_id()
    if text.startswith("/start"):
        if not owner:
            db.set_setting("telegram.chat_id", chat)
            owner = chat
            db.log_event("config", "founder", "Telegram linked to Zax")
        await send("⚡ Zax online. You're connected, Founder. Message me to delegate work, "
                   "ask for /status, or just talk. I'll ping you when tasks land.", to=chat)
        return

    if owner and chat != owner:
        await send("This Zax bot is privately linked to its Founder.", to=chat)
        return
    if not owner:
        await send("Send /start first to link this chat.", to=chat)
        return

    from . import ceo  # local import avoids a circular dependency at load time

    if text.startswith("/status"):
        st = ceo.org_state()
        await send(f"📊 {st['headcount']} agents · perf {st['avg_performance']}% · "
                   f"{st['tasks'].get('inbox', 0) + st['tasks'].get('assigned', 0)} in pipeline · "
                   f"{st['tasks'].get('done', 0)} done")
        return
    if text.startswith("/tasks"):
        rows = db.all_tasks(8)
        lines = [f"{'✅' if t['status']=='done' else '⏳'} {t['title']} "
                 f"({t['progress']}%{', '+str(t['score'])+'/100' if t['score'] is not None else ''})"
                 for t in rows] or ["(no tasks yet)"]
        await send("🗂 Recent tasks:\n" + "\n".join(lines))
        return

    # Anything else is a conversation turn with Zax (can delegate, hire, fire…).
    await _call("sendChatAction", {"chat_id": owner, "action": "typing"}, timeout=10)
    res = await ceo.chat(text, session_id=SESSION)
    await send(f"🤖 {res['reply']}")


async def _poll_updates() -> None:
    offset = 0
    while True:
        try:
            data = await _call("getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    with contextlib.suppress(Exception):
                        await _handle_message(upd["message"])
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)  # transient network/API error — back off


# ---------------------------------------------------------------- event push

NOTIFY_KINDS = {"hire", "fire", "complete", "review", "throttle"}
EMOJI = {"hire": "🟢", "fire": "🔴", "complete": "✅", "review": "📝", "throttle": "⚠️"}


async def _poll_events() -> None:
    # start from the latest id so we don't replay history on connect
    rows = db.events_since(0, limit=1)
    last_id = rows[0]["id"] if rows else 0
    while True:
        try:
            await asyncio.sleep(6)
            if not notify_enabled() or not chat_id():
                rows = db.events_since(last_id, limit=1)
                if rows:
                    last_id = rows[0]["id"]
                continue
            events = list(reversed(db.events_since(last_id, limit=40)))
            for e in events:
                last_id = max(last_id, e["id"])
                # NOTIFY_KINDS is the intended filter; don't restrict by actor — e.g.
                # "complete" events are logged under the agent's name, not zax/founder.
                if e["kind"] in NOTIFY_KINDS:
                    await send(f"{EMOJI.get(e['kind'], '•')} {e['message']}")
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)


# ---------------------------------------------------------------- lifecycle

async def _run() -> None:
    await asyncio.gather(_poll_updates(), _poll_events())


def start() -> None:
    global _task
    if _task and not _task.done():
        return
    if not configured():
        return
    _task = asyncio.get_event_loop().create_task(_run())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
