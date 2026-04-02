#!/usr/bin/env python3
"""Entry point for the agent supervisor service."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

# Ensure parent package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import all agent types so they register themselves
from agents.supervisor import AgentSupervisor  # noqa: E402
from agents.health_monitor import *  # noqa: E402,F401,F403
from agents.index_maintainer import *  # noqa: E402,F401,F403
from agents.code_watcher import *  # noqa: E402,F401,F403
from agents.research_agent import *  # noqa: E402,F401,F403
from agents.news_agent import *  # noqa: E402,F401,F403

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_api_key() -> str:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    keys = cfg.get("gateway", {}).get("api_keys", [])
    return keys[0] if keys else ""


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    api_key = load_api_key()
    supervisor = AgentSupervisor("http://localhost:8100", api_key)
    await supervisor.start()

    # Run until signaled
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await supervisor.stop()


if __name__ == "__main__":
    asyncio.run(main())
