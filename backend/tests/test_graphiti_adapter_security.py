import asyncio
from types import SimpleNamespace

from graphiti_core.nodes import EntityNode

from app.services.graphiti_adapter import _GraphitiBackend, _NodeNamespace


class CapturingDriver:
    def __init__(self):
        self.calls = []

    async def execute_query(self, query, **params):
        self.calls.append((query, params))


def test_delete_group_only_matches_graphiti_nodes():
    driver = CapturingDriver()
    backend = _GraphitiBackend.__new__(_GraphitiBackend)
    backend.graphiti = SimpleNamespace(driver=driver)

    asyncio.run(backend.delete_group("group-a"))

    query, params = driver.calls[0]
    assert params == {
        "group_id": "group-a",
        "managed_labels": ["Entity", "Episodic", "Community"],
    }
    assert "any(label IN labels(n) WHERE label IN $managed_labels)" in query
    assert "DETACH DELETE n" in query


def test_entity_lookup_does_not_return_node_from_another_group(monkeypatch):
    node = SimpleNamespace(
        uuid="entity-a",
        name="Entity A",
        labels=["Entity"],
        summary="",
        attributes={},
        created_at=None,
        group_id="group-b",
    )
    backend = SimpleNamespace(
        graphiti=SimpleNamespace(driver=object()),
        run=lambda _coroutine: node,
    )
    monkeypatch.setattr(EntityNode, "get_by_uuid", lambda *_args: None)

    result = _NodeNamespace(backend).get(uuid_="entity-a", graph_id="group-a")

    assert result is None


def test_entity_lookup_returns_node_from_requested_group(monkeypatch):
    node = SimpleNamespace(
        uuid="entity-a",
        name="Entity A",
        labels=["Entity"],
        summary="",
        attributes={},
        created_at=None,
        group_id="group-a",
    )
    backend = SimpleNamespace(
        graphiti=SimpleNamespace(driver=object()),
        run=lambda _coroutine: node,
    )
    monkeypatch.setattr(EntityNode, "get_by_uuid", lambda *_args: None)

    result = _NodeNamespace(backend).get(uuid_="entity-a", graph_id="group-a")

    assert result is not None
    assert result.group_id == "group-a"
