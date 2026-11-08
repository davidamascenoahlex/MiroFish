import asyncio
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.services import simulation_runner as runner_module
from app.services.simulation_ipc import SimulationIPCClient
from app.services.simulation_runner import RunnerStatus, SimulationRunner, SimulationRunState


@pytest.fixture
def terminal_run(monkeypatch, tmp_path):
    sim = tmp_path / "sim-recovery"
    sim.mkdir()
    state = SimulationRunState(
        simulation_id=sim.name, runner_status=RunnerStatus.STOPPED,
        current_round=40, total_rounds=40,
        twitter_actions_count=210, reddit_actions_count=245,
    )
    (sim / "simulation_config.json").write_text("{}")
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationRunner, "_interview_processes", {}, raising=False)
    monkeypatch.setattr(SimulationRunner, "get_run_state", classmethod(lambda cls, sid: state))
    monkeypatch.setattr(SimulationRunner, "_save_run_state", Mock(side_effect=AssertionError("run state changed")))
    monkeypatch.setattr(runner_module.ZepGraphMemoryManager, "get_updater", lambda sid: None)
    return sim, state


def test_live_environment_is_reused_without_launch(monkeypatch, terminal_run):
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: True))
    launch = Mock(side_effect=AssertionError("unexpected new process"))
    monkeypatch.setattr(runner_module.subprocess, "Popen", launch)
    SimulationRunner.ensure_interview_env(terminal_run[0].name)
    launch.assert_not_called()


def test_terminal_environment_launches_interviews_only(monkeypatch, terminal_run):
    sim, state = terminal_run
    process = SimpleNamespace(poll=lambda: None, pid=4567)
    launch = Mock(return_value=process)
    monkeypatch.setattr(runner_module.subprocess, "Popen", launch)
    alive = iter([False, True])
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: next(alive)))
    SimulationRunner.ensure_interview_env(sim.name)
    args, kwargs = launch.call_args
    assert Path(args[0][2]).name == "restore_interview_env.py"
    assert "--max-rounds" not in args[0]
    assert kwargs["cwd"] == str(sim)
    assert Path(kwargs["stdout"].name).name == "interview.log"
    assert state.runner_status == RunnerStatus.STOPPED
    assert state.current_round == 40
    assert state.to_dict()["total_actions_count"] == 455


@pytest.mark.parametrize("status", [RunnerStatus.IDLE, RunnerStatus.STARTING, RunnerStatus.RUNNING, RunnerStatus.STOPPING, RunnerStatus.FAILED])
def test_nonterminal_or_failed_run_is_not_replayed(monkeypatch, terminal_run, status):
    sim, state = terminal_run
    state.runner_status = status
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: False))
    monkeypatch.setattr(runner_module.subprocess, "Popen", Mock(side_effect=AssertionError("unexpected process")))
    with pytest.raises(ValueError):
        SimulationRunner.ensure_interview_env(sim.name)


def test_pending_ingestion_blocks_recovery(monkeypatch, terminal_run):
    sim, _ = terminal_run
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: False))
    monkeypatch.setattr(runner_module.ZepGraphMemoryManager, "get_updater", lambda sid: object())
    with pytest.raises(ValueError):
        SimulationRunner.ensure_interview_env(sim.name)


def test_old_pending_commands_are_not_replayed(monkeypatch, terminal_run):
    sim, _ = terminal_run
    (sim / "ipc_commands").mkdir()
    (sim / "ipc_commands" / "old.json").write_text("{}")
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: False))
    with pytest.raises(ValueError, match="pending"):
        SimulationRunner.ensure_interview_env(sim.name)
    assert (sim / "ipc_commands" / "old.json").exists()


def test_failed_worker_surfaces_failure_without_changing_run(monkeypatch, terminal_run):
    sim, state = terminal_run
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: False))
    monkeypatch.setattr(runner_module.subprocess, "Popen", Mock(return_value=SimpleNamespace(poll=lambda: 1, pid=9)))
    with pytest.raises(RuntimeError, match="interview.log"):
        SimulationRunner.ensure_interview_env(sim.name)
    assert state.runner_status == RunnerStatus.STOPPED


@pytest.mark.parametrize("api,body,method", [
    (simulation_api.interview_agent, {"agent_id": 3, "prompt": "Question"}, "interview_agent"),
    (simulation_api.interview_agents_batch, {"interviews": [{"agent_id": 3, "prompt": "Question"}]}, "interview_agents_batch"),
    (simulation_api.interview_all_agents, {"prompt": "Question"}, "interview_all_agents"),
])
def test_api_delegates_closed_environment_to_shared_recovery(monkeypatch, api, body, method):
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: False))
    call = Mock(return_value={"success": True, "result": {}})
    monkeypatch.setattr(SimulationRunner, method, call)
    app = Flask(__name__)
    with app.test_request_context(json={"simulation_id": "sim-recovery", **body}):
        response = api()
    assert not isinstance(response, tuple)
    assert response.get_json()["success"]
    call.assert_called_once()


def test_stale_recovered_environment_is_not_alive(tmp_path):
    client = SimulationIPCClient(str(tmp_path))
    status = {"status": "alive", "mode": "interview", "timestamp": (datetime.now() - timedelta(seconds=60)).isoformat()}
    (tmp_path / "env_status.json").write_text(json.dumps(status))
    assert not client.check_env_alive()
    status["timestamp"] = datetime.now().isoformat()
    (tmp_path / "env_status.json").write_text(json.dumps(status))
    assert client.check_env_alive()


def load_recovery(monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("restore_interview_env", scripts / "restore_interview_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("platform", ["twitter", "reddit"])
def test_reopen_preserves_db_and_rehydrates_recorded_context(monkeypatch, tmp_path, platform):
    monkeypatch.chdir(tmp_path)
    module = load_recovery(monkeypatch)
    db_path = tmp_path / f"{platform}_simulation.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE user (user_id INTEGER, name TEXT)")
        db.execute("INSERT INTO user VALUES (0, 'owner')")
        db.execute("CREATE TABLE trace (user_id INTEGER, created_at TEXT, action TEXT, info TEXT)")
        db.execute("INSERT INTO trace VALUES (0, '40', 'create_post', ?)", ('{"content":"keep stock reliable"}',))
    profile = "twitter_profiles.csv" if platform == "twitter" else "reddit_profiles.json"
    (tmp_path / profile).write_text("profile")
    before = db_path.read_bytes()
    agent = SimpleNamespace(user_info=SimpleNamespace(name="owner"), update_memory=Mock())
    graph = SimpleNamespace(get_agents=lambda: [(0, agent)])
    env = SimpleNamespace(
        channel=object(), platform=SimpleNamespace(running=AsyncMock(), sandbox_clock=SimpleNamespace(time_step=0)),
        reset=AsyncMock(side_effect=AssertionError("must not sign up/reset")),
    )
    monkeypatch.setattr(module, "create_model", lambda *a, **k: object())
    monkeypatch.setattr(module, f"generate_{platform}_agent_graph", AsyncMock(return_value=graph))
    monkeypatch.setattr(module, "connect_platform_channel", Mock(return_value=graph))
    monkeypatch.setattr(module.oasis, "make", Mock(return_value=env))
    async def reopen():
        result = await module.restore_platform({}, str(tmp_path), platform)
        await result.env.platform_task
        return result
    result = asyncio.run(reopen())
    assert result.env is env
    env.reset.assert_not_called()
    assert db_path.read_bytes() == before
    assert "keep stock reliable" in agent.update_memory.call_args.kwargs["message"].content
    module.connect_platform_channel.assert_called_once_with(channel=env.channel, agent_graph=graph)


def test_missing_database_is_not_created(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    module = load_recovery(monkeypatch)
    with pytest.raises(FileNotFoundError):
        asyncio.run(module.restore_platform({}, str(tmp_path), "reddit"))
    assert not (tmp_path / "reddit_simulation.db").exists()


@pytest.mark.parametrize("simulation_id", ["", "..", "../another", "one/two", "one\\two"])
def test_recovery_rejects_nonlocal_identifiers(simulation_id):
    with pytest.raises(ValueError, match="Invalid simulation ID"):
        SimulationRunner.ensure_interview_env(simulation_id)


def test_startup_timeout_closes_owned_worker(monkeypatch, terminal_run):
    sim, state = terminal_run
    process = SimpleNamespace(poll=lambda: None, pid=4567)
    monkeypatch.setattr(SimulationRunner, "check_env_alive", classmethod(lambda cls, sid: False))
    monkeypatch.setattr(runner_module.subprocess, "Popen", Mock(return_value=process))
    terminate = Mock()
    monkeypatch.setattr(SimulationRunner, "_terminate_process", terminate)
    with pytest.raises(TimeoutError):
        SimulationRunner.ensure_interview_env(sim.name, timeout=0)
    terminate.assert_called_once_with(process, sim.name, timeout=5)
    assert sim.name not in SimulationRunner._interview_processes
    assert state.runner_status == RunnerStatus.STOPPED


@pytest.mark.parametrize("method,kwargs,ipc_method", [
    ("interview_agent", {"agent_id": 3, "prompt": "Question"}, "send_interview"),
    ("interview_agents_batch", {"interviews": [{"agent_id": 3, "prompt": "Question"}]}, "send_batch_interview"),
])
def test_runner_reopens_before_sending_interview(monkeypatch, terminal_run, method, kwargs, ipc_method):
    sim, _ = terminal_run
    events = []
    monkeypatch.setattr(SimulationRunner, "ensure_interview_env", classmethod(lambda cls, sid: events.append("restore")))
    response = SimpleNamespace(status=SimpleNamespace(value="completed"), result={}, timestamp="now")
    def send(*args, **kwargs):
        events.append("send")
        return response
    monkeypatch.setattr(SimulationIPCClient, ipc_method, send)
    assert getattr(SimulationRunner, method)(sim.name, **kwargs)["success"]
    assert events == ["restore", "send"]


def test_shutdown_closes_interviews_without_changing_terminal_state(monkeypatch, terminal_run):
    sim, state = terminal_run
    process = SimpleNamespace(poll=lambda: None, pid=4567)
    SimulationRunner._interview_processes[sim.name] = process
    monkeypatch.setattr(SimulationRunner, "_cleanup_done", False)
    monkeypatch.setattr(SimulationRunner, "_processes", {})
    monkeypatch.setattr(SimulationRunner, "_graph_memory_enabled", {})
    monkeypatch.setattr(runner_module.ZepGraphMemoryManager, "get_simulation_ids", lambda: [])
    terminate = Mock()
    monkeypatch.setattr(SimulationRunner, "_terminate_process", terminate)
    SimulationRunner.cleanup_all_simulations()
    terminate.assert_called_once_with(process, sim.name, timeout=5)
    assert not SimulationRunner._interview_processes
    assert state.runner_status == RunnerStatus.STOPPED


def test_latest_interview_uses_append_order_not_restarted_clock(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    module = load_recovery(monkeypatch)
    db_path = tmp_path / "reddit_simulation.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE trace (user_id INTEGER, created_at INTEGER, action TEXT, info TEXT)")
        db.execute("INSERT INTO trace VALUES (3, 900, 'interview', ?)", ('{"response":"old"}',))
        db.execute("INSERT INTO trace VALUES (3, 1, 'interview', ?)", ('{"response":"new"}',))
    handler = module.ParallelIPCHandler(str(tmp_path))
    assert handler._get_interview_result(3, "reddit")["response"] == "new"


def test_restart_cleanup_cannot_delete_data_under_interview_worker(monkeypatch, terminal_run):
    sim, _ = terminal_run
    SimulationRunner._interview_processes[sim.name] = SimpleNamespace(poll=lambda: None)
    protected = sim / "reddit_simulation.db"
    protected.write_bytes(b"persisted simulation")
    response = SimulationRunner.cleanup_simulation_logs(sim.name)
    assert not response["success"]
    assert response["cleaned_files"] == []
    assert protected.read_bytes() == b"persisted simulation"


def test_previous_worker_readiness_does_not_confirm_replacement(tmp_path):
    client = SimulationIPCClient(str(tmp_path))
    (tmp_path / "env_status.json").write_text(json.dumps({
        "status": "alive", "mode": "interview", "pid": 100,
        "timestamp": datetime.now().isoformat(),
    }))
    assert not client.check_env_alive(expected_pid=200)
    assert client.check_env_alive(expected_pid=100)
