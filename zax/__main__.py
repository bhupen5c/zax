#!/usr/bin/env python3
"""Zax — resilient entrypoint that auto-restarts on crash."""
import sys
import time

from . import config

if __name__ == "__main__":
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            import uvicorn
            uvicorn.run("zax.main:app", host=config.HOST, port=config.PORT)
            break  # clean exit
        except SystemExit:
            break  # intentional shutdown
        except KeyboardInterrupt:
            break  # user quit
        except Exception as exc:
            print(f"Zax crashed (attempt {attempt}/{max_retries}): {exc}", file=sys.stderr)
            if attempt < max_retries:
                wait = min(attempt * 5, 30)
                print(f"Restarting in {wait}s…", file=sys.stderr)
                time.sleep(wait)
            else:
                print("Zax failed to stay running after all retries.", file=sys.stderr)
                raise
