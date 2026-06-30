"""Agent tools (the Odysseus side): web search, URL fetch, workspace files, memory, code execution.

Shell execution is available but sandboxed to the workspace directory.
"""
import asyncio
import html
import ipaddress
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from . import config, db

# Shared httpx client (same pattern as llm.py) to prevent connection leaks
_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        limits = httpx.Limits(max_keepalive_connections=4, max_connections=10)
        _shared_client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(30.0))
    return _shared_client


TOOL_SPECS = """You may use tools. To call one, respond with ONLY a JSON object:
  {"tool": "<name>", "args": {...}}
Available tools:
  web_search   {"query": str}          -> top web results (title, url, snippet)
  fetch_url    {"url": str}            -> readable text of a web page
  write_file   {"path": str, "content": str} -> save a file in your workspace
  read_file    {"path": str}           -> read a file from your workspace
  list_files   {"path": str}           -> list files in your workspace
  run_code     {"code": str}           -> run Python in an isolated process (opt-in; stdout returned)
  shell        {"command": str}        -> run a shell command in the workspace (opt-in)
  remember     {"note": str}           -> store a fact in company memory
Tools are OPTIONAL — many tasks need none; if you can answer directly, just do.
You have a small, limited tool budget, so use it sparingly (one or two searches/
fetches is usually plenty) and synthesize from what you gather.
When you are finished, respond with ONLY: {"final": "<your complete deliverable>"}"""


def _strip_tags(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _safe_path(rel: str) -> Path:
    p = (config.WORKSPACE_DIR / rel).resolve()
    if not p.is_relative_to(config.WORKSPACE_DIR.resolve()):
        raise ValueError("path escapes the workspace")
    return p


def _assert_public_url(url: str) -> None:
    """SSRF guard: agents process untrusted web content and could be prompt-injected
    into fetching internal services — refuse anything that resolves to a
    private / loopback / link-local / metadata address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are allowed")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"cannot resolve host: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"blocked non-public address: {host} -> {ip}")


def _tavily_key() -> str:
    """Tavily key from the Settings store or the TAVILY_API_KEY env var (either works)."""
    return (db.get_setting("tools.tavily_api_key", "") or os.environ.get("TAVILY_API_KEY", "")).strip()


async def _tavily_search(query: str, key: str) -> str:
    """Tavily — purpose-built for AI research: ranked results WITH extracted page
    content, so the agent cites primary sources instead of whatever SEO blog a raw
    scrape surfaces. This is what lifts research-quality toward a deep browser."""
    r = await _get_client().post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "search_depth": "advanced",
              "max_results": 6, "include_answer": False, "include_raw_content": False},
        timeout=httpx.Timeout(30.0),
    )
    r.raise_for_status()
    results = []
    for item in r.json().get("results", []):
        title = _strip_tags(item.get("title", ""))
        url = item.get("url", "")
        content = _strip_tags(item.get("content", ""))
        results.append(f"- {title}\n  {url}\n  {content[:400]}")
    return "\n".join(results) or "No results."


async def _ddg_search(query: str) -> str:
    """Free fallback: scrape DuckDuckGo's HTML results (title/url/snippet only)."""
    r = await _get_client().post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (zax-agent)"},
    )
    r.raise_for_status()
    results = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(?:.*?class="result__snippet"[^>]*>(.*?)</a>)?',
        r.text,
        re.S,
    ):
        url, title, snippet = m.group(1), _strip_tags(m.group(2)), _strip_tags(m.group(3) or "")
        results.append(f"- {title}\n  {url}\n  {snippet[:200]}")
        if len(results) >= 6:
            break
    return "\n".join(results) or "No results."


async def web_search(query: str) -> str:
    """Web search. Uses Tavily (AI-research-grade source quality) when a key is set
    (Settings → tools.tavily_api_key, or env TAVILY_API_KEY); otherwise falls back to
    a free DuckDuckGo HTML scrape so search always works."""
    key = _tavily_key()
    if key:
        try:
            return await _tavily_search(query, key)
        except Exception:
            pass  # quota/network/API change — degrade gracefully to the free scrape
    return await _ddg_search(query)


async def fetch_url(url: str) -> str:
    # Redirects are followed manually so every hop gets the SSRF check.
    client = _get_client()
    for _ in range(4):
        _assert_public_url(url)
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (zax-agent)"})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        r.raise_for_status()
        return _strip_tags(r.text)[:6000]
    return "Too many redirects."


def write_file(path: str, content: str) -> str:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} chars to workspace/{path}"


def read_file(path: str) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"No such file: workspace/{path}"
    return p.read_text()[:8000]


def list_files(path: str = ".") -> str:
    """List files in the agent's workspace (a shared skill-tool)."""
    base = _safe_path(path)
    if not base.exists():
        return f"No such path: workspace/{path}"
    if base.is_file():
        return f"{path} ({base.stat().st_size} bytes)"
    entries = []
    for p in sorted(base.iterdir()):
        rel = p.relative_to(config.WORKSPACE_DIR)
        entries.append(f"{rel}/" if p.is_dir() else f"{rel} ({p.stat().st_size}b)")
    return "\n".join(entries) or "(empty)"


async def run_code(code: str) -> str:
    """Execute Python in a SEPARATE process (isolated from the server), with a
    timeout. Real code execution can touch the filesystem/network, so it is
    opt-in (ZAX_ALLOW_CODE=1) — agents otherwise deliver code via write_file.
    Note: this is process isolation + a time limit, NOT a security sandbox."""
    if not config.ALLOW_CODE:
        return ("Code execution is disabled. Write your code to a file with write_file "
                "instead (set ZAX_ALLOW_CODE=1 to enable running it).")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", code,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(config.WORKSPACE_DIR),
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(config.WORKSPACE_DIR)},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=config.CODE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return f"(code timed out after {config.CODE_TIMEOUT}s)"
    text = out.decode(errors="replace").strip()
    return text[:6000] or "(ran with no output)"


async def shell(command: str) -> str:
    """Run a shell command in the workspace directory. Fully gated behind
    ZAX_ALLOW_SHELL — there is no prefix allowlist, because command chaining
    (`ls; rm -rf ~`) and absolute paths (`cat /etc/passwd`) defeat one."""
    if not config.ALLOW_SHELL:
        return ("Shell access is disabled. Use write_file/read_file/list_files for files. "
                "To enable the shell, set ZAX_ALLOW_SHELL=1.")
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(config.WORKSPACE_DIR),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return out.decode(errors="replace")[:6000]
    except asyncio.TimeoutError:
        proc.kill()
        return "(command timed out after 30s)"


async def run(name: str, args: dict, agent_name: str = "agent") -> str:
    try:
        if name == "web_search":
            return await web_search(str(args.get("query", "")))
        if name == "fetch_url":
            return await fetch_url(str(args.get("url", "")))
        if name == "write_file":
            return write_file(str(args.get("path", "out.txt")), str(args.get("content", "")))
        if name == "read_file":
            return read_file(str(args.get("path", "")))
        if name == "list_files":
            return list_files(str(args.get("path", ".")))
        if name == "run_code":
            return await run_code(str(args.get("code", "")))
        if name == "shell":
            return await shell(str(args.get("command", "")))
        if name == "remember":
            db.remember(str(args.get("note", ""))[:1000], kind="note", agent=agent_name)
            return "Stored in company memory."
        return f"Unknown tool: {name}"
    except Exception as exc:  # tool errors go back to the model, not up the stack
        return f"Tool error: {exc}"
