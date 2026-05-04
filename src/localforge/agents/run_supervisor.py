#!/usr/bin/env python3
"""Entry point for the agent supervisor service (standalone mode).

When the gateway is running, agents start inside the gateway process.
This entry point is for running the supervisor as a separate service.
"""

import asyncio
import logging
import os
import signal

import yaml

# Import from the localforge package (installed or editable)
import localforge.agents.code_watcher  # noqa: F401
import localforge.agents.daily_digest  # noqa: F401
import localforge.agents.health_monitor  # noqa: F401
import localforge.agents.index_maintainer  # noqa: F401
import localforge.agents.news_agent  # noqa: F401
import localforge.agents.research_agent  # noqa: F401
import localforge.agents.yaml_schema_validator  # noqa: F401
from localforge.agents.supervisor import AgentSupervisor
from localforge.paths import config_path


def load_api_key() -> str:
    cfg_file = config_path()
    if cfg_file.exists():
        with open(cfg_file) as f:
            cfg = yaml.safe_load(f) or {}
        keys = cfg.get("gateway", {}).get("api_keys", [])
        return keys[0] if keys else ""
    return ""


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    api_key = load_api_key()
    gateway_url = os.environ.get("LOCALFORGE_GATEWAY_URL", "http://localhost:8100")
    supervisor = AgentSupervisor(gateway_url, api_key)
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
