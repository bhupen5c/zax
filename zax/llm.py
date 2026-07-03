"""Provider-agnostic LLM access.

Built-in support for every major hosted API (Anthropic, OpenAI, Google Gemini,
OpenRouter, Groq, Mistral, DeepSeek, xAI, Together), local models via Ollama,
ANY other OpenAI-compatible endpoint via the `custom` provider, and — the default
whenever it is installed — `claude-cli`, which shells out to the Claude Code
terminal login so Zax runs on the Founder's Anthropic *subscription* rather than
a metered API key.

Keys and models are configured at runtime from the Settings panel (persisted in
SQLite), with environment variables as fallback. chat() returns (text, tokens).
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
from typing import Optional

import httpx

from . import config, db

# Shared httpx client for all provider calls — connection pooling prevents
# file descriptor exhaustion and macOS from killing the process.
_shared_client: httpx.AsyncClient | None = None
_last_working_provider: str = ""  # set by chat() after a successful call


def effective_provider() -> str:
    """Return the provider that last actually succeeded, or resolve_provider()."""
    return _last_working_provider or resolve_provider()


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        limits = httpx.Limits(max_keepalive_connections=4, max_connections=10)
        # Generous read timeout — big free-tier models (e.g. Nemotron Ultra 550B on
        # OpenRouter) can take well over a minute to start streaming a reply.
        _shared_client = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(300.0, connect=15.0),
        )
    return _shared_client


async def _close_client() -> None:
    global _shared_client
    if _shared_client:
        await _shared_client.aclose()
        _shared_client = None


PROVIDERS: dict[str, dict] = {
    "claude-cli": {
        "label": "Claude · subscription",
        "kind": "cli",
        "default_model": "sonnet",
        "models": ["sonnet", "opus", "haiku"],
        "tiers": {"deep": "opus", "fast": "haiku"},
        "desc": "Uses your Anthropic subscription through the `claude` terminal login — "
                "no API key. Run `claude login` once. Models: sonnet, opus, haiku, or a full model id.",
    },
    "anthropic": {
        "label": "Anthropic API",
        "kind": "anthropic",
        "env": "ANTHROPIC_API_KEY",
        "base": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-6",
        "models": ["claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001"],
        "tiers": {"deep": "claude-opus-4-8", "fast": "claude-haiku-4-5-20251001"},
        "desc": "Claude models via metered API key.",
    },
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "env": "OPENAI_API_KEY",
        "base": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
        "tiers": {"deep": "o3", "fast": "gpt-4o-mini"},
        "desc": "GPT models.",
    },
    "google": {
        "label": "Google Gemini",
        "kind": "openai",
        "env": "GEMINI_API_KEY",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
        "models": ["gemini-2.0-pro", "gemini-2.0-flash"],
        "tiers": {"deep": "gemini-2.0-pro", "fast": "gemini-2.0-flash"},
        "desc": "Gemini models via Google's OpenAI-compatible endpoint.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "kind": "openai",
        "env": "OPENROUTER_API_KEY",
        "base": "https://openrouter.ai/api/v1",
        "default_model": "openrouter/auto",
        "desc": "One key for 400+ models from every lab (Claude, GPT, Gemini, Llama, …).",
    },
    "groq": {
        "label": "Groq",
        "kind": "openai",
        "env": "GROQ_API_KEY",
        "base": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "models": ["llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b"],
        "tiers": {"deep": "deepseek-r1-distill-llama-70b", "fast": "llama-3.3-70b-versatile"},
        "desc": "Ultra-fast open models on Groq hardware.",
    },
    "mistral": {
        "label": "Mistral",
        "kind": "openai",
        "env": "MISTRAL_API_KEY",
        "base": "https://api.mistral.ai/v1",
        "default_model": "mistral-large-latest",
        "models": ["mistral-large-latest", "mistral-small-latest"],
        "tiers": {"deep": "mistral-large-latest", "fast": "mistral-small-latest"},
        "desc": "Mistral models.",
    },
    "deepseek": {
        "label": "DeepSeek",
        "kind": "openai",
        "env": "DEEPSEEK_API_KEY",
        "base": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-pro",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "tiers": {"deep": "deepseek-v4-pro", "fast": "deepseek-v4-flash"},
        "desc": "DeepSeek models (deepseek-v4-pro / deepseek-v4-flash).",
    },
    "xai": {
        "label": "xAI Grok",
        "kind": "openai",
        "env": "XAI_API_KEY",
        "base": "https://api.x.ai/v1",
        "default_model": "grok-3",
        "models": ["grok-3", "grok-3-mini"],
        "tiers": {"deep": "grok-3", "fast": "grok-3-mini"},
        "desc": "Grok models.",
    },
    "together": {
        "label": "Together AI",
        "kind": "openai",
        "env": "TOGETHER_API_KEY",
        "base": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "desc": "Open-source models (Llama, Qwen, Mixtral, …).",
    },
    "ollama": {
        "label": "Ollama · local",
        "kind": "ollama",
        "default_model": "llama3.1",
        "desc": "Fully local and private — requires Ollama running on this machine.",
    },
    "custom": {
        "label": "Custom endpoint",
        "kind": "openai",
        "base": "",
        "default_model": "",
        "desc": "Any OpenAI-compatible API (LM Studio, vLLM, llama.cpp, Perplexity, Cerebras, Fireworks, …): "
                "set base URL, key, and model.",
    },
    "moonshot": {
        "label": "Moonshot AI (Kimi)",
        "kind": "openai",
        "env": "MOONSHOT_API_KEY",
        "base": "https://api.moonshot.ai/v1",
        "default_model": "moonshot-v1-8k",
        "desc": "Moonshot AI (Kimi) models via API key.",
    },
    "mock": {
        "label": "Mock core",
        "kind": "mock",
        "default_model": "zax-mock-1",
        "desc": "No intelligence — canned replies so the org machinery can be demoed offline.",
    },
}

# Priority when auto-detecting: subscription first, then keyed APIs.
AUTO_ORDER = ["claude-cli", "anthropic", "openai", "google", "openrouter", "groq",
              "mistral", "deepseek", "xai", "together", "moonshot"]


# ---------------------------------------------------------------- configuration

def _setting(key: str) -> str:
    try:
        return db.get_setting(key, "")
    except Exception:
        return ""


def cli_available() -> bool:
    return shutil.which("claude") is not None


def api_key(pid: str) -> str:
    spec = PROVIDERS[pid]
    return _setting(f"provider.{pid}.api_key") or os.environ.get(spec.get("env", ""), "")


def base_url(pid: str) -> str:
    if pid == "custom":
        return _setting("provider.custom.base_url").rstrip("/")
    if pid == "ollama":
        return config.OLLAMA_URL.rstrip("/")
    return PROVIDERS[pid].get("base", "")


def model_for(pid: str) -> str:
    return (
        _setting(f"provider.{pid}.model")
        or config.ZAX_MODEL
        or PROVIDERS[pid]["default_model"]
    )


def is_configured(pid: str) -> bool:
    kind = PROVIDERS[pid]["kind"]
    if kind == "cli":
        return cli_available()
    if kind in ("mock", "ollama"):
        return True
    if pid == "custom":
        return bool(base_url(pid) and model_for(pid))
    return bool(api_key(pid))


def set_core(pid: str, model: str = "") -> str:
    """Switch the active provider (and optionally its model) — used by Zax's chat-level
    set_core action. Clears the sticky last-working-provider so the status badge and
    the next call reflect the switch immediately. Returns the now-effective model."""
    global _last_working_provider
    db.set_setting("provider.active", pid)
    if model:
        db.set_setting(f"provider.{pid}.model", model.strip())
    _last_working_provider = ""
    return model_for(pid)


def core_options() -> str:
    """Compact, prompt-friendly inventory of providers/models so Zax can switch cores
    from chat without inventing names. '►' marks the active provider."""
    active = resolve_provider()
    lines = []
    for pid, spec in PROVIDERS.items():
        if pid == "mock":
            continue
        mark = "►" if pid == active else " "
        ready = "ready" if is_configured(pid) else "NO KEY"
        models = ", ".join(spec.get("models") or [m for m in [spec.get("default_model")] if m]) or "any model id"
        tiers = spec.get("tiers")
        tier_s = f" | reasoning tiers: deep={tiers['deep']}, fast={tiers['fast']}" if tiers else ""
        lines.append(f"{mark} {pid} ({spec['label']}, {ready}) current={model_for(pid)} | models: {models}{tier_s}")
    return "\n".join(lines)


async def test(pid: str = "") -> tuple[bool, str]:
    """Quick connectivity test for a provider. Returns (ok, message)."""
    pid = pid or effective_provider()
    if pid == "mock":
        return True, "Mock core always online"
    if pid == "ollama":
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{base_url(pid)}/api/tags")
                return r.is_success, "Ollama online" if r.is_success else f"Ollama returned {r.status_code}"
        except Exception as e:
            return False, str(e)
    if pid == "claude-cli":
        if not cli_available():
            return False, "claude CLI not found"
        return True, "claude CLI available (tested on next chat)"
    try:
        text, tokens = await chat(
            "You are a connectivity probe. Reply with one short sentence.",
            [{"role": "user", "content": "Confirm you are online."}],
            max_tokens=50,
            provider=pid,
        )
        return True, text.strip()[:200]
    except Exception as e:
        return False, str(e)[:200]


def resolve_provider() -> str:
    if config.PROVIDER != "auto" and config.PROVIDER in PROVIDERS:
        return config.PROVIDER
    chosen = _setting("provider.active")
    if chosen in PROVIDERS and is_configured(chosen):
        return chosen
    for pid in AUTO_ORDER:
        if is_configured(pid):
            return pid
    return "mock"


def _next_available(skip_pid: str) -> str:
    """Find the next configured provider after skip_pid, falling back to mock."""
    for pid in AUTO_ORDER:
        if pid != skip_pid and is_configured(pid):
            return pid
    return "mock"


def default_model(pid: str = "") -> str:
    return model_for(pid or resolve_provider())


def provider_overview() -> list[dict]:
    active = resolve_provider()
    out = []
    for pid, spec in PROVIDERS.items():
        key = _setting(f"provider.{pid}.api_key")
        env_key = os.environ.get(spec.get("env", ""), "")
        out.append({
            "id": pid,
            "label": spec["label"],
            "desc": spec["desc"],
            "kind": spec["kind"],
            "active": pid == active,
            "configured": is_configured(pid),
            "needs_key": spec["kind"] in ("anthropic", "openai") and pid != "custom" or pid == "custom",
            "key_hint": f"…{key[-4:]}" if key else ("from environment" if env_key else ""),
            "model": model_for(pid),
            "default_model": spec["default_model"],
            "models": spec.get("models") or ([spec["default_model"]] if spec["default_model"] else []),
            "tiers": spec.get("tiers") or {},
            "base_url": base_url(pid) if pid in ("custom", "ollama") else "",
            "cli_found": cli_available() if pid == "claude-cli" else None,
        })
    return out


# ---------------------------------------------------------------- json helper

def extract_json(text: str) -> Optional[dict]:
    """Find the first balanced JSON object in a blob of model output.

    Brace counting is string-aware (braces inside "..." don't count, so answers
    containing { } parse correctly), and json.loads runs with strict=False so a
    model's unescaped newlines inside a string are tolerated."""
    # strip a ```json fence if present
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1), strict=False)
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1], strict=False)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------- chat

async def chat(system: str, messages: list[dict], model: str = "",
               max_tokens: int = 1500, provider: str = "") -> tuple[str, int]:
    global _last_working_provider
    pid = provider or resolve_provider()
    spec = PROVIDERS.get(pid) or PROVIDERS["mock"]
    mdl = model or model_for(pid)
    kind = spec["kind"]
    if kind == "cli":
        try:
            result = await _claude_cli(system, messages, mdl)
            _last_working_provider = pid
            return result
        except RuntimeError as exc:
            # Session limit or auth failure on claude-cli — auto-fallback to
            # the next best provider so the app keeps working.
            low = str(exc).lower()
            if "session limit" in low or "auth" in low or "log" in low:
                fallback = _next_available(pid)
                if fallback:
                    db.log_event("info", "llm",
                                 f"claude-cli unavailable ({str(exc)[:80]}), "
                                 f"falling back to {fallback}")
                    result = await chat(system, messages, model, max_tokens, provider=fallback)
                    _last_working_provider = fallback
                    return result
            raise
    if kind == "anthropic":
        result = await _anthropic(system, messages, mdl, max_tokens)
        _last_working_provider = pid
        return result
    if kind == "openai":
        result = await _openai_compat(pid, system, messages, mdl, max_tokens)
        _last_working_provider = pid
        return result
    if kind == "ollama":
        result = await _ollama(system, messages, mdl)
        _last_working_provider = pid
        return result
    result = _mock(system, messages)
    _last_working_provider = pid
    return result


# ------------------------------------------------ claude-cli (subscription)

async def _claude_cli(system: str, messages: list[dict], model: str) -> tuple[str, int]:
    if not cli_available():
        raise RuntimeError("`claude` CLI not found — install Claude Code and run `claude login`")
    turns = []
    for m in messages:
        who = "User" if m["role"] == "user" else "Assistant"
        turns.append(f"{who}:\n{m['content']}")
    prompt = "\n\n".join(turns) + "\n\nRespond now as the Assistant. Output only your reply."

    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--model", model or "sonnet",
        # The CLI is an agent with its own tools; Zax only wants a completion.
        # Without this it can spend its turn budget on tool calls and return nothing.
        "--system-prompt", system + "\n\nIMPORTANT: You have no tools in this context. "
                                    "Never attempt tool use — answer directly, in one turn.",
        "--max-turns", "2",
    ]
    # Run with a minimal whitelisted environment so the CLI always authenticates
    # via its own `claude login` (subscription) — never an inherited API key,
    # proxy base URL, or a parent Claude Code session's credentials.
    keep = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config.DATA_DIR),
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=300)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        raise RuntimeError("claude CLI timed out")

    raw = out.decode(errors="replace").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if data is None:
        if proc.returncode != 0:
            detail = (err.decode(errors="replace").strip() or raw)
            raise RuntimeError(f"claude CLI failed: {detail[:400]}")
        return raw, 0

    text = str(data.get("result", "")).strip()
    if data.get("is_error"):
        detail = text or f"claude CLI error (subtype: {data.get('subtype', 'unknown')})"
        low = detail.lower()
        hint = (" → run `claude login` in your terminal once, then retry"
                if ("log" in low or "auth" in low) else "")
        is_session_limit = "session limit" in low
        if is_session_limit:
            hint = " — auto-switching provider. Set up an API key or wait for the session to reset."
        raise RuntimeError(f"{detail[:300]}{hint}")
    usage = data.get("usage") or {}
    tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return text, tokens


# ------------------------------------------------ shared error surfacing

def _raise_for_api(r, pid: str) -> None:
    """Surface the provider's actual error message instead of a generic HTTP 400."""
    if r.is_success:
        return
    detail = ""
    try:
        body = r.json()
        err = body.get("error", body)
        detail = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        detail = (r.text or "")[:300]
    msg = f"{pid} error {r.status_code}: {detail or 'request rejected'}"
    if r.status_code == 400 and ("model" in (detail or "").lower() or not detail):
        msg += (" — check the model id in Settings (most providers need an exact slug, "
                "e.g. OpenRouter wants 'vendor/model' like 'nvidia/nemotron-nano-9b-v2')")
    if r.status_code in (401, 403):
        msg += " — check your API key in Settings"
    raise RuntimeError(msg)


# ------------------------------------------------ anthropic API

async def _anthropic(system: str, messages: list[dict], model: str, max_tokens: int) -> tuple[str, int]:
    client = _get_client()
    r = await client.post(
        f"{base_url('anthropic')}/v1/messages",
        headers={
            "x-api-key": api_key("anthropic"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": model, "max_tokens": max_tokens, "system": system, "messages": messages},
    )
    _raise_for_api(r, "anthropic")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []))
    usage = data.get("usage", {})
    return text, usage.get("input_tokens", 0) + usage.get("output_tokens", 0)


# ------------------------------------------------ any OpenAI-compatible API

async def _openai_compat(pid: str, system: str, messages: list[dict],
                         model: str, max_tokens: int) -> tuple[str, int]:
    base = base_url(pid)
    if not base:
        raise RuntimeError(f"{pid}: no base URL configured")
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    headers = {"Authorization": f"Bearer {api_key(pid)}"}
    if pid == "openrouter":
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "Zax"
    client = _get_client()
    r = await client.post(f"{base}/chat/completions", headers=headers, json=body)
    if r.status_code == 400 and "max_completion_tokens" in r.text:
        # newer OpenAI models renamed the cap parameter
        body.pop("max_tokens", None)
        body["max_completion_tokens"] = max_tokens
        r = await client.post(f"{base}/chat/completions", headers=headers, json=body)
    _raise_for_api(r, pid)
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        # some gateways return {"error": ...} with a 200 status
        raise RuntimeError(f"{pid}: {data.get('error', 'no completion returned')}")
    msg = choices[0].get("message", {})
    text = (msg.get("content") or "").strip()
    if not text:
        finish = (choices[0].get("finish_reason") or "")
        cot = (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
        if cot and finish == "length":
            # Reasoning model spent the whole budget thinking and got cut off before
            # writing the answer. NEVER surface the raw chain-of-thought as the reply
            # (it leaks into chat as "We need to respond as ZAX…") — fail loudly so
            # the caller retries/raises the cap.
            raise RuntimeError(f"{pid}: {model} spent the entire {max_tokens}-token "
                               f"budget on hidden reasoning — raise max_tokens")
        # Model genuinely finished but left its text in the reasoning field
        # (e.g. Nemotron) — that IS the answer, keep the old fallback.
        text = cot
    if not text:
        finish = (choices[0].get("finish_reason") or "")
        raise RuntimeError(f"{pid}: empty response from model"
                           + (f" (finish_reason={finish})" if finish else ""))
    return text, data.get("usage", {}).get("total_tokens", 0)


# ------------------------------------------------ ollama (local)

async def _ollama(system: str, messages: list[dict], model: str) -> tuple[str, int]:
    client = _get_client()
    r = await client.post(
        f"{base_url('ollama')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, *messages],
            # Ollama defaults to a tiny context (2-4k); Zax's agent prompts (persona +
            # rules + memory + task) overflow it and some backends 500 outright.
            "options": {"num_ctx": 8192},
        },
    )
    r.raise_for_status()
    data = r.json()
    tokens = data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
    return data.get("message", {}).get("content", ""), tokens


# ---------------------------------------------------------------- mock provider

def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def _mock(system: str, messages: list[dict]) -> tuple[str, int]:
    last = messages[-1]["content"] if messages else ""
    low = last.lower()

    # NB: order matters — these prompt fingerprints overlap on generic words.
    if "daily reflection" in system.lower():
        return (
            json.dumps({
                "report": "The org shipped its queue with acceptable quality. Research outputs "
                          "were strongest; writing tasks need tighter openings. No personnel "
                          "changes required today.",
                "lessons": ["Lead every deliverable with the bottom line, then the supporting detail."],
                "coaching": {},
            }),
            0,
        )
    if "distilling one piece of experience" in system.lower():
        return (
            json.dumps({"text": "When delivering research, cite at least two sources with "
                                "concrete numbers and lead with a single clear recommendation."}),
            0,
        )
    if "respond with json" in system.lower() and "score" in system.lower():
        score = 60 + _seed(last) % 36  # deterministic 60-95 review score
        return (
            json.dumps({"score": score, "feedback": "Acceptable work. Tighten the summary next time."}),
            0,
        )
    if "hiring brief" in system.lower():
        return (
            json.dumps(
                {
                    "name": "Nova",
                    "title": "Generalist Operative",
                    "role": "general research and writing",
                    "persona": "A fast, pragmatic generalist who answers concisely and cites assumptions.",
                }
            ),
            0,
        )
    if '"tool"' in system or "TOOL RESULT" in last:
        return (
            json.dumps({"final": f"[mock] Task handled offline. Summary: {last[:160]}"}),
            0,
        )
    if "status" in low or "report" in low:
        return (
            "All systems nominal, Founder. The org is executing. Pick a real intelligence core in "
            "Settings — Claude subscription, any API provider, or local Ollama. I am on the mock core.",
            0,
        )
    return (
        "Acknowledged, Founder. I am running on the mock core — open Settings to wire me to a real "
        "model (Claude subscription via terminal login, any API key, or local Ollama). The org "
        "machinery, hiring, firing, and task pipeline are fully live.",
        0,
    )
