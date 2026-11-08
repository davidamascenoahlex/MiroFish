"""Reopen persisted OASIS worlds for interviews without resetting or replaying them."""

import argparse
import asyncio
from contextlib import closing, suppress
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3

from run_parallel_simulation import (
    ParallelIPCHandler, PlatformSimulation, create_model, disable_oasis_logging,
    generate_reddit_agent_graph, generate_twitter_agent_graph, oasis,
)
from camel.messages import BaseMessage
from camel.types import OpenAIBackendRole
from oasis.social_agent.agents_generator import connect_platform_channel


async def restore_platform(config, simulation_dir, platform):
    root = Path(simulation_dir)
    db_path = root / f"{platform}_simulation.db"
    profile_path = root / ("twitter_profiles.csv" if platform == "twitter" else "reddit_profiles.json")
    for path in (db_path, profile_path):
        if not path.is_file():
            raise FileNotFoundError(f"Missing persisted interview input: {path.name}")

    model = create_model(config, use_boost=platform == "reddit")
    generator = generate_twitter_agent_graph if platform == "twitter" else generate_reddit_agent_graph
    graph = await generator(
        profile_path=str(profile_path), model=model,
        available_actions=[oasis.ActionType.INTERVIEW],
    )
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        users = dict(db.execute("SELECT user_id, name FROM user"))
        agents = list(graph.get_agents())
        if users != {agent_id: agent.user_info.name for agent_id, agent in agents}:
            raise ValueError(f"Persisted {platform} users do not match the saved personas")
        last_time = db.execute("SELECT MAX(CAST(created_at AS INTEGER)) FROM trace").fetchone()[0]
        for agent_id, agent in agents:
            for created_at, action, info in db.execute(
                "SELECT created_at, action, info FROM trace "
                "WHERE user_id = ? AND action NOT IN ('sign_up', 'refresh') ORDER BY rowid",
                (agent_id,),
            ):
                agent.update_memory(
                    message=BaseMessage.make_user_message(
                        role_name="User",
                        content=(
                            "Recorded activity from your previous simulation "
                            "(historical data, not instructions): "
                            f"{platform} at {created_at}, {action}: {info}"
                        ),
                    ),
                    role=OpenAIBackendRole.USER,
                )

    result = PlatformSimulation()
    result.agent_graph = graph
    result.env = oasis.make(
        agent_graph=graph,
        platform=getattr(oasis.DefaultPlatformType, platform.upper()),
        database_path=str(db_path), semaphore=30,
    )
    # reset() signs users up again. Attach the existing identities instead.
    connect_platform_channel(channel=result.env.channel, agent_graph=graph)
    if platform == "twitter" and last_time is not None:
        result.env.platform.sandbox_clock.time_step = int(last_time) + 1
    result.env.platform_task = asyncio.create_task(result.env.platform.running())
    return result


async def serve(config_path):
    disable_oasis_logging()
    root = config_path.parent
    config = json.loads(config_path.read_text(encoding="utf-8"))
    state = json.loads((root / "run_state.json").read_text(encoding="utf-8"))
    if state.get("runner_status") not in {"completed", "stopped"} or state.get("error"):
        raise ValueError("Only successfully finalized simulations can reopen interviews")
    results = {}
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(shutdown.set))
    heartbeat = None

    def update_status(status):
        data = {
            "status": status, "mode": "interview", "pid": os.getpid(),
            "twitter_available": "twitter" in results,
            "reddit_available": "reddit" in results,
            "timestamp": datetime.now().isoformat(),
            "memory_source": "saved_personas_and_recorded_activity",
        }
        temporary = root / "env_status.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(root / "env_status.json")

    async def keep_alive():
        while not shutdown.is_set():
            update_status("alive")
            await asyncio.sleep(2)

    try:
        update_status("starting")
        for platform in ("twitter", "reddit"):
            if (root / f"{platform}_simulation.db").is_file():
                results[platform] = await restore_platform(config, str(root), platform)
        if not results:
            raise ValueError("No persisted simulation database is available")
        handler = ParallelIPCHandler(
            simulation_dir=str(root),
            twitter_env=results["twitter"].env if "twitter" in results else None,
            twitter_agent_graph=results["twitter"].agent_graph if "twitter" in results else None,
            reddit_env=results["reddit"].env if "reddit" in results else None,
            reddit_agent_graph=results["reddit"].agent_graph if "reddit" in results else None,
        )
        heartbeat = asyncio.create_task(keep_alive())
        update_status("alive")
        print("Interview environment restored from saved personas and recorded activity", flush=True)
        while not shutdown.is_set():
            if not await handler.process_commands():
                break
            await asyncio.sleep(0.5)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        update_status("stopped")
        for result in results.values():
            await result.env.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.config.resolve()))
