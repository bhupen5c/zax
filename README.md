# ZAX — your AI CEO

[![tests](https://github.com/bhupen5c/zax/actions/workflows/ci.yml/badge.svg)](https://github.com/bhupen5c/zax/actions/workflows/ci.yml)

> You are the Founder. **Zax** is the CEO. Zax hires a staff of AI agents, assigns them
> your day-to-day tasks, scores every deliverable, and **fires the ones who underperform**.
> It greets you out loud in a deep voice when you boot it — JARVIS, but he runs your company.

Zax fuses three open-source ideas into one app:

| Source | What Zax took from it |
|---|---|
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | The "AI company" layer — org chart, agents as employees with roles/titles, task tickets with audit trail, heartbeat execution, per-agent monthly token budgets with throttling, performance tracking, hiring/firing governance, recurring routines |
| [pewdiepie-archdaemon/odysseus](https://github.com/pewdiepie-archdaemon/odysseus) | The self-hosted AI workspace layer — chat UI, multi-provider LLM support (Anthropic / OpenAI / Ollama local), agents with real tools (web search, URL fetch, workspace files), persistent company memory with full-text recall, voice |
| [safishamsi/graphify](https://github.com/safishamsi/graphify) | The **knowledge-graph memory** layer (vendored under `vendor/graphify`) — chats, tasks, and memories are distilled into a graph of entities + relationships; graphify's own retrieval engine (TF-IDF node scoring → seed → BFS subgraph → token-budgeted render) pulls back just the relevant subgraph so the model reads facts from the graph **instead of replaying the whole transcript** — fewer tokens, longer memory |

## Quick start

```bash
git clone https://github.com/bhupen5c/zax.git
cd zax
cp .env.example .env        # optional — keys can also be added live in Settings
./run.sh                    # creates a venv, installs deps, boots Zax
```

Open **http://127.0.0.1:8777**, press **INITIALIZE** — Zax welcomes you.

No API key? Zax runs on a built-in **mock core** so the whole org machinery (hiring,
firing, tasks, reviews) works as a demo. For real intelligence, pick any provider in
**Settings → Intelligence Core** (Claude subscription via the `claude` CLI, or an API
key for OpenAI / DeepSeek / OpenRouter / Groq / Gemini / …, or local Ollama).

## Intelligence core — runs on anything

Zax is provider-agnostic. Pick and configure the brain live from **Settings →
Intelligence Core** (no restart needed):

| Provider | Auth | Notes |
|---|---|---|
| **Claude · subscription** | `claude login` (terminal) | **Default when the Claude Code CLI is installed.** Uses your Anthropic subscription — no API key, no metered billing. Models: `sonnet`, `opus`, `haiku`. |
| Anthropic API | API key | Metered Claude API |
| OpenAI | API key | GPT models |
| Google Gemini | API key | via Google's OpenAI-compatible endpoint |
| OpenRouter | API key | one key → 400+ models from every lab |
| Groq / Mistral / DeepSeek / xAI / Together | API key | each lab's native API |
| Ollama | none | fully local & private |
| **Custom endpoint** | optional | **any** OpenAI-compatible API: LM Studio, vLLM, llama.cpp, Perplexity, Cerebras, Fireworks… just set base URL + model |
| Mock core | none | offline demo of the org machinery |

Every provider row has **SAVE** (key/model/base-URL, stored locally in SQLite) and
**TEST** (live connectivity probe). Env vars (see `.env.example`) work as fallbacks.

## What happens when it runs

1. **First boot** — Zax hires the founding team: **Atlas** (research), **Lyra** (writing),
   **Cipher** (operations).
2. **You delegate** — type or *speak* to Zax on the Bridge ("research the best NAS under
   $500", "draft a cold email to investors") or file tickets on the Tasks board. Zax
   emits actions from chat: it can queue tasks, hire, and fire on your word.
3. **The heartbeat** (every 20s) — Zax assigns inbox tickets to the best-matched agent;
   agents execute with tools (web search, page fetch, workspace files, memory); Zax
   reviews each deliverable and scores it 0–100.
4. **HR happens automatically** — every score updates a rolling performance number.
   Drop below **50% after 3+ tasks → Zax fires the agent** (you'll hear about it).
   Backlog grows beyond capacity → **Zax drafts a hiring brief and hires** a new
   specialist (capped at `ZAX_MAX_HEADCOUNT`). Token budgets are enforced monthly;
   over budget → throttled.
5. **Routines** — recurring work ("daily news brief, every 1440 min") becomes a ticket
   on schedule without you asking twice.
6. **The org learns from itself** — two AI-powered loops feed a persistent,
   Odysseus-style memory bank (the **Cortex** panel):
   - *Per-review distillation*: every task scored ≥85 becomes a reusable **skill**;
     every score <60 becomes a corrective **lesson** — distilled by the active model,
     tagged to the agent, stored forever.
   - *Daily reflection*: once a day Zax reviews the last 24h of org activity, writes
     an executive report, extracts org-wide lessons, and leaves coaching notes for
     individual agents (also on demand via **REFLECT NOW**).
   Recall is hybrid (keyword rank × importance × recency, usage-tracked), and the top
   matches are injected back into every agent's prompt before it works — so mistakes
   don't repeat and wins compound. Memory survives restarts (SQLite + FTS5); agents
   can also `remember` facts explicitly, and Zax recalls memory in chat.
7. **The memory graph reduces tokens** (the **Graph** tab, graphify engine) — every
   chat turn and completed task is distilled into a knowledge graph of entities and
   relationships (`EXTRACTED`/`INFERRED` confidence, graphify's tags). When you ask
   Zax something, only the *relevant subgraph* is retrieved and injected into the
   prompt (capped to a token budget), and the raw history sent shrinks from 16 turns
   to 6. The model answers from the graph — even to attribute-phrased questions like
   "which database does my product use?" — instead of re-reading the transcript.
   Interactive force-directed visualization, "ask the graph" query, top-hub ("god
   node") detection, and one-click rebuild. Exports graphify-compatible
   `data/graph.json` (servable via `python -m graphify.serve data/graph.json`).

## The Zax voice

Zax speaks with a custom deep profile: server-side **edge-tts** neural voice pitched
down (`en-US-ChristopherNeural`, −18Hz pitch, −6% rate). If the server voice is
unavailable (offline), the browser falls back to an equivalent pitched-down Web Speech
profile, so Zax always has a voice. Voice input (the 🎙 button) uses the browser's
speech recognition — Chrome recommended.

## Architecture

```
zax/
├── run.sh                 one-command launcher
├── requirements.txt       fastapi, uvicorn, httpx, edge-tts, python-dotenv
├── .env.example           all knobs documented
└── zax/
    ├── main.py            FastAPI app + lifespan (seeds org, starts heartbeat)
    ├── api.py             HTTP routes (thin glue)
    ├── ceo.py             Zax's brain: chat+actions, assignment, reviews, hire/fire
    ├── agents.py          agent task execution (tool loop)
    ├── heartbeat.py       the org tick: routines → assign → execute → review → HR
    ├── tools.py           web_search, fetch_url, read/write workspace, remember, shell*
    ├── llm.py             Anthropic / OpenAI / Ollama / mock providers
    ├── voice.py           deep-voice TTS (edge-tts)
    ├── db.py              SQLite: agents, tasks, events, chat, routines, FTS memory
    ├── config.py          env-driven settings
    ├── prompts/           Zax's system/review/hiring prompts (reviewable as text)
    └── static/            the HUD (vanilla JS, no build step)
```

*Shell tool is **disabled by default** (`ZAX_ALLOW_SHELL=1` to enable) — agents don't
get a shell on your machine unless you say so. File tools are jailed to `data/workspace/`.

## Security & safety

- **Local only by default** (`127.0.0.1`). Outbound traffic: your chosen LLM API,
  DuckDuckGo search, pages agents fetch, and Microsoft's edge-tts service for the
  Zax voice (reply text is sent there for synthesis; disable voice or uninstall
  edge-tts for a fully silent install).
- **Local API hardening**: POSTs must be `application/json` (forces CORS preflight,
  which fails — defeats cross-site request forgery from malicious websites against
  your localhost), and a Host allowlist defeats DNS rebinding while bound to loopback.
- **SSRF guard**: agent `fetch_url` resolves every hop and refuses private, loopback,
  link-local, and cloud-metadata addresses — a prompt-injected agent can't probe your
  machine or network.
- **Workspace jail**: agent file tools are confined to `data/workspace/`
  (path-resolution checked, traversal and symlink escapes blocked).
- **Shell off by default** (`ZAX_ALLOW_SHELL=1` to opt in).
- **Failure containment**: if the intelligence core fails (bad key, logged out),
  tasks are requeued — never burned — and the heartbeat backs off for 5 minutes.
- **API keys** entered in Settings are stored plaintext in the local SQLite file
  (`data/zax.db`) — standard for local dev tools, but know it's there.
- Zax's hire/fire authority is bounded by config: max headcount, fire threshold,
  minimum task sample before firing, and per-agent token budgets.
- The full audit log (every hire, fire, assignment, tool call, review) is on the
  **Operations** panel.

## Testing

```bash
./run.sh                              # one-time, to create the venv + install deps
.venv/bin/python -m pytest            # 97 unit tests across every module
./scripts/smoke_test.sh               # 58 live API + security-hardening checks (server running)
```

The unit suite runs against a fresh temp DB and the mock provider — no keys or network
needed. GitHub Actions runs it on every push (see `.github/workflows/ci.yml`).

## Deploy (Railway / Render / Fly)

Zax is a **stateful, always-on server** (SQLite state + background loops), so it needs a
host that runs a persistent container with a small disk — **not** a serverless platform
like Vercel (there, state resets and delegated tasks never execute). A `Dockerfile` is
included; it reads the host's `$PORT`, binds `0.0.0.0`, and keeps all state in `/data`.

**Railway (recommended):**
1. New Project → **Deploy from GitHub repo** → pick `bhupen5c/zax` (Railway uses the Dockerfile).
2. **Storage** — pick one:
   - **Supabase Postgres (recommended):** set `DATABASE_URL` to your Supabase connection
     string (Dashboard → Connect → Session pooler URI). No volume needed; managed +
     backed up. Zax auto-creates its schema on first boot.
   - **SQLite + volume:** add a **Volume** mounted at **`/data`** and set `ZAX_DATA_DIR=/data`
     (persists `zax.db` across deploys).
3. **Variables:**
   - `DATABASE_URL=postgresql://…` (Supabase) — *or* `ZAX_DATA_DIR=/data` with a volume
   - `ZAX_ACCESS_PASSWORD=<a strong password>` — **required**; gates the public URL behind
     HTTP Basic auth (username can be anything) so strangers can't drive your org or spend
     your tokens.
   - a provider key, e.g. `DEEPSEEK_API_KEY=...` (or `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, …) —
     or leave it out and add the key in **Settings → Intelligence Core** after first boot.
   - optional: `ZAX_FOUNDER_NAME=...`
4. Deploy. Health check is `/healthz`. Open the generated URL, sign in with your password,
   press **INITIALIZE**.

`ZAX_HOST=0.0.0.0` and `/data` are already set by the Dockerfile. Render and Fly.io work the
same way (Docker + a persistent disk/volume at `/data`).

> The `claude-cli` subscription provider only works where the `claude` CLI is logged in
> (your Mac) — in the cloud use an API-key provider (DeepSeek, OpenAI, OpenRouter, …) or Ollama.

## License

Zax is licensed under the **GNU AGPL-3.0** (see [`LICENSE`](LICENSE)). It vendors
[graphify](https://github.com/safishamsi/graphify) (AGPL-3.0) under `vendor/graphify` and
draws on [paperclip](https://github.com/paperclipai/paperclip) and
[odysseus](https://github.com/pewdiepie-archdaemon/odysseus) (both AGPL-3.0); the copyleft
license carries through. If you run a modified version as a network service, the AGPL
requires you to offer users its source.
