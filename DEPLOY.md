# Deploy Zax to Railway (reach it from anywhere)

Zax runs great locally via launchd, but to use it from your phone or share it, deploy the
container to Railway. The repo is deploy-ready — `Dockerfile`, `railway.json` (healthcheck +
restart policy), and a `/healthz` endpoint are already wired.

## One-time setup (~5 minutes)

1. **Push is already done** — Railway deploys from `github.com/bhupen5c/zax`.
2. Go to **railway.app → New Project → Deploy from GitHub repo → `bhupen5c/zax`**.
   Railway detects the `Dockerfile` and builds it automatically.
3. **Add a Volume** (critical — without it, every redeploy wipes the SQLite DB):
   Service → **Variables/Settings → Volumes → New Volume**, mount path **`/data`**.
   (`ZAX_DATA_DIR=/data` is already set in the Dockerfile, so the DB, workspace, and
   settings live on the volume.)
4. **Set environment variables** (Service → Variables):

   | Variable | Value | Why |
   |---|---|---|
   | `ZAX_ACCESS_PASSWORD` | *a strong password* | **Required.** The app refuses to boot public without it. You'll enter it once in the browser (HTTP Basic). |
   | `DEEPSEEK_API_KEY` | *your key* | The intelligence core. (Or `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) |
   | `TAVILY_API_KEY` | *your key* | Research-grade search (optional but recommended). |
   | `ZAX_FOUNDER_NAME` | `Bhupen` | How Zax addresses you. |

   Railway injects `PORT` automatically — `config.py` already honors it.
5. **Generate a domain**: Service → Settings → **Networking → Generate Domain**.
   Open the URL, enter your `ZAX_ACCESS_PASSWORD`, and Zax is live.

## Notes
- **Durability**: the volume at `/data` keeps everything across redeploys. Alternatively set
  `DATABASE_URL` to a Supabase/Postgres string and Zax uses that instead (dual-backend).
- **Local models won't work in the cloud** — Ollama (`ornith`/`gemma4`) is on your Mac only.
  Pick a cloud core (DeepSeek/Anthropic/OpenAI) in the ⚛ dropdown or via env.
- **Lock it down further** (optional): set `ZAX_ALLOW_SHELL=0` and/or `ZAX_ALLOW_CODE=0` to
  disable command/code execution on the public instance.
- **Safety rail**: booting public (`ZAX_HOST=0.0.0.0`) with no `ZAX_ACCESS_PASSWORD` is refused
  outright (override only with `ZAX_ALLOW_INSECURE=1` — don't).
- **Auto-deploy**: every `git push` to `main` redeploys automatically.

## CI (add once, via GitHub web UI)
`.github/workflows/ci.yml` is on disk but can't be pushed by the CLI token (missing `workflow`
scope). Add it once through GitHub's web editor (New file → paste the contents) and tests run on
every push.
