#!/usr/bin/env bash
# One-command launcher with auto-restart: creates a venv, installs deps, boots Zax.
# If the server crashes, it restarts up to 5 times with increasing delay.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

MAX_RETRIES=5
RETRY_DELAY=5
for ((i=1; i<=MAX_RETRIES; i++)); do
  echo "== Zax boot attempt $i =="
  if python -m zax; then
    echo "Zax exited cleanly."
    exit 0
  fi
  echo "Zax crashed (attempt $i/$MAX_RETRIES)."
  if [ $i -lt $MAX_RETRIES ]; then
    echo "Restarting in ${RETRY_DELAY}s…"
    sleep $RETRY_DELAY
    RETRY_DELAY=$((RETRY_DELAY + 5))
  fi
done
echo "Zax failed to stay running after $MAX_RETRIES attempts."
exit 1
