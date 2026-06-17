# Zax — Project Guide (for Claude & humans)

> Zax is an AI CEO that runs the Founder's day-to-day: it hires AI agents, assigns them
> tasks, scores every deliverable, and fires underperformers. JARVIS-style voice UX.
> Feature fusion of paperclipai/paperclip (AI-company orchestration) and
> pewdiepie-archdaemon/odysseus (self-hosted AI workspace).

## Commands
| Task | Command |
|------|---------|
| Run | `./run.sh` (creates venv, installs, boots) |
| Dev | `.venv/bin/python -m zax` |
| URL | http://127.0.0.1:8777 |

## Architecture
- `zax/ceo.py` — Zax's brain: chat + `<action>` blocks, task assignment, reviews
  (0–100 scores → EMA performance), hire/fire policy. Start here.
- `zax/heartbeat.py` — the org tick: daily reflection → routines → assign → execute →
  review → HR pass.
- `zax/graph.py` — knowledge-graph memory; embeds the vendored graphify engine
  (`vendor/graphify`, added to sys.path; only needs networkx at import — tree-sitter
  imports are lazy). Distils chat/tasks/memories into graph_nodes/graph_edges (SQLite),
  materializes a NetworkX graph, and reuses graphify's `serve._score_nodes /
  _pick_seeds / _bfs / _subgraph_to_text` for token-budgeted retrieval. `context_block()`
  is injected into Zax + agent prompts to replace full-history replay (token saver).
  `_augment_seeds()` adds summary/relation matching on top of graphify's label-only
  scoring so attribute-phrased questions resolve. UI: the Graph tab (canvas force viz).
  NOTE: graphify scores node *labels* — extraction (prompts/graph_extract.txt) emits
  category concept nodes ("payment", "database") + "is a" edges so attributes are findable.
- `zax/learning.py` — self-learning: per-review skill/lesson distillation
  (score ≥85 / <60), daily reflection (report + lessons + per-agent coaching), and
  context_for() which injects recalled memory into agent prompts. Memory bank lives
  in db.memories (+FTS5 external-content index, synced by triggers); recall is
  keyword-rank × importance × recency, agent-tagged memories stay private to that
  agent. UI: the Cortex panel.
- `zax/agents.py` — agent execution: JSON tool loop (`{"tool":...}` / `{"final":...}`).
- `zax/llm.py` — provider registry: claude-cli (Anthropic SUBSCRIPTION via `claude -p`
  terminal login — the preferred default; it strips ANTHROPIC_API_KEY from the
  subprocess env on purpose), anthropic / openai / google / openrouter / groq /
  mistral / deepseek / xai / together (OpenAI-compatible path), ollama, custom
  (any OpenAI-compatible base URL), mock. Runtime config lives in the SQLite
  `settings` table (`provider.active`, `provider.<id>.api_key|model`), env vars are
  fallback only. Mock keeps the app fully demoable with no API keys; never break that.
- `zax/db.py` — SQLite (agents, tasks, events, messages, routines, FTS5 memory).
- `zax/voice.py` — the deep "Zax voice": edge-tts, pitch −18Hz. UI falls back to
  Web Speech API (pitch 0.5) when server TTS is unavailable.
- `zax/static/` — vanilla JS HUD, no build step. `zax/prompts/` — prompt text files;
  they use `{placeholder}` substitution via `.replace()`, NOT `str.format()`
  (the prompts contain literal JSON braces).

## Rules
- Agent file tools stay jailed to `data/workspace/`. Shell tool stays behind
  `ZAX_ALLOW_SHELL` (default off).
- Zax's HR authority stays bounded by config: `ZAX_MAX_HEADCOUNT`,
  `ZAX_FIRE_THRESHOLD`, `ZAX_MIN_TASKS_BEFORE_FIRE`, per-agent token budgets.
- Every org mutation (hire/fire/assign/review/throttle) logs to `events` — the audit
  trail on the Operations panel is a feature, keep it complete.
