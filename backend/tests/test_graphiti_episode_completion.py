import asyncio
from types import SimpleNamespace

import pytest
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodicNode

from app.services import graphiti_adapter
from app.services import zep_graph_memory_updater as updater_module
from app.services.zep_graph_memory_updater import AgentActivity, ZepGraphMemoryUpdater


def _graph(monkeypatch, *, group_id="graph-a", error=None):
    node = SimpleNamespace(uuid="episode-1", group_id=group_id)
    reads = []
    writes = []
    driver = object()

    async def get_by_uuid(actual_driver, uuid):
        assert actual_driver is driver
        reads.append(uuid)
        if error is not None:
            raise error
        return node

    async def add_episode(**kwargs):
        writes.append(kwargs)
        return SimpleNamespace(episode=node)

    monkeypatch.setattr(EpisodicNode, "get_by_uuid", get_by_uuid)
    backend = SimpleNamespace(
        graphiti=SimpleNamespace(driver=driver, add_episode=add_episode),
        run=asyncio.run,
        get_ontology=lambda _graph_id: (None, None, None),
    )
    return graphiti_adapter._GraphNamespace(backend), reads, writes


def test_persisted_episode_is_processed_and_exposes_both_uuid_aliases(monkeypatch):
    graph, reads, _ = _graph(monkeypatch)

    episode = graph.episode.get(uuid_="episode-1", graph_id="graph-a")

    assert episode.uuid == episode.uuid_ == "episode-1"
    assert episode.group_id == "graph-a"
    assert episode.processed is True
    assert reads == ["episode-1"]


@pytest.mark.parametrize("graph_id", [None, "", " "])
def test_episode_lookup_requires_a_graph(monkeypatch, graph_id):
    graph, reads, _ = _graph(monkeypatch)

    with pytest.raises(ValueError, match="graph_id is required"):
        graph.episode.get(uuid_="episode-1", graph_id=graph_id)

    assert reads == []


@pytest.mark.parametrize("uuid", [None, "", " "])
def test_episode_lookup_requires_a_uuid(monkeypatch, uuid):
    graph, reads, _ = _graph(monkeypatch)

    with pytest.raises(ValueError, match="episode UUID is required"):
        graph.episode.get(uuid_=uuid, graph_id="graph-a")

    assert reads == []


def test_episode_from_another_graph_cannot_confirm_completion(monkeypatch):
    graph, _, _ = _graph(monkeypatch, group_id="graph-b")

    with pytest.raises(graphiti_adapter.NotFoundError):
        graph.episode.get(uuid_="episode-1", graph_id="graph-a")


def test_missing_episode_cannot_confirm_completion(monkeypatch):
    error = NodeNotFoundError("episode-1")
    graph, _, _ = _graph(monkeypatch, error=error)

    with pytest.raises(graphiti_adapter.NotFoundError) as caught:
        graph.episode.get(uuid_="episode-1", graph_id="graph-a")

    assert caught.value.__cause__ is error


def test_database_failure_is_not_reported_as_processed(monkeypatch):
    error = RuntimeError("database unavailable")
    graph, _, _ = _graph(monkeypatch, error=error)

    with pytest.raises(RuntimeError, match="database unavailable") as caught:
        graph.episode.get(uuid_="episode-1", graph_id="graph-a")

    assert caught.value is error


def test_simulation_drain_uses_real_adapter_without_replaying_writes(monkeypatch):
    graph, reads, writes = _graph(monkeypatch)
    monkeypatch.setattr(
        updater_module, "get_graphiti_client", lambda: SimpleNamespace(graph=graph)
    )
    updater = ZepGraphMemoryUpdater("graph-a", simulation_id="sim-a")
    activity = AgentActivity(
        platform="twitter",
        agent_id=1,
        agent_name="Agent",
        action_type="CREATE_POST",
        action_args={"content": "hello"},
        round_num=1,
        timestamp="2026-11-08T12:00:00+00:00",
    )
    updater._send_batch_activities([activity], "twitter")
    assert updater.get_stats()["pending_episode_count"] == 1

    updater.stop()
    updater.stop()

    assert len(writes) == 1
    assert writes[0]["group_id"] == "graph-a"
    assert reads == ["episode-1"]
    assert updater.get_stats()["pending_episode_count"] == 0
    assert updater.get_stats()["items_sent"] == 1
    assert updater.get_stats()["failed_count"] == 0


def test_failed_episode_confirmation_remains_pending(monkeypatch):
    graph, _, _ = _graph(monkeypatch, error=ValueError("invalid database response"))
    monkeypatch.setattr(
        updater_module, "get_graphiti_client", lambda: SimpleNamespace(graph=graph)
    )
    updater = ZepGraphMemoryUpdater("graph-a", simulation_id="sim-a")
    updater._pending_episode_uuids = ["episode-1"]

    with pytest.raises(ValueError, match="invalid database response"):
        updater.stop()

    assert updater.get_stats()["pending_episode_count"] == 1
