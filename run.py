"""
TRON-X startup launcher.

Use this instead of calling uvicorn directly, so the Windows Proactor event
loop is set BEFORE uvicorn creates any asyncio infrastructure.

    python run.py            # prod (port 8000, no reload)
    python run.py --reload   # dev (hot-reload, port 8000)
    python run.py --port 9000
"""
import sys
import asyncio

# MUST happen before any asyncio event loop is created.
# uvicorn's CLI phase can trigger loop creation, so we do this first.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start TRON-X")
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--port",   type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "src.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
