#!/usr/bin/env python3
"""
WebSocket test client — streams a build and renders output in colour.
Usage: python3 scripts/test_ws.py [--host localhost] [--token ptb-dev-changeme]
"""
import argparse
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

COLORS = {
    "token":   "\033[0m",
    "status":  "\033[2;36m",
    "done":    "\033[1;32m",
    "error":   "\033[1;31m",
    "divider": "\033[2;37m",
    "started": "\033[1;34m",
    "warning": "\033[33m",
}
RESET = "\033[0m"

BUILD_REQUEST = {
    "account_id": "PLATFORM",
    "project_id": "PROJECT-TEST",
    "region": "uk-1",
    "prompt": "Build a simple coffee shop ordering agent. Handle drink orders, customisations, and FAQs about the menu.",
}

async def main(host: str, token: str):
    url = f"ws://{host}/ws/build?token={token}"
    print(f"Connecting to {url}…\n")

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(BUILD_REQUEST))

        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type", "")
            color = COLORS.get(kind, "")

            if kind == "token":
                print(color + msg.get("text", "") + RESET, end="", flush=True)
            elif kind == "done":
                print(f"\n\n{color}✅ Done! Branch: {msg.get('branch')}{RESET}")
            elif kind == "error":
                print(f"\n{color}❌ {msg.get('message')}{RESET}")
            elif kind == "divider":
                print(f"\n{color}{msg.get('message')}{RESET}")
            else:
                print(f"\n{color}[{msg.get('ts','')}] {msg.get('message','')}{RESET}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host",  default="localhost:8788")
    parser.add_argument("--token", default="ptb-dev-changeme")
    args = parser.parse_args()
    asyncio.run(main(args.host, args.token))
