from types import SimpleNamespace

import pytest

from app.services.graph_builder import BatchSubmission, GraphBuilderService
from app.services.graphiti_adapter import _NodeNamespace
from app.services.oasis_profile_generator import OasisProfileGenerator
from app.services.zep_entity_reader import EntityNode, ZepEntityReader
from app.services.zep_tools import ZepToolsService


def test_report_search_caps_query_before_graphiti_call():
    calls = []

    class GraphApi:
        def search(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(edges=[], nodes=[])

    service = object.__new__(ZepToolsService)
    service.client = SimpleNamespace(graph=GraphApi())
    query = "q" * 401
    result = service.search_graph("graph-id", query)

    assert calls[0]["query"] == query[:400]
    assert result.query == query


def test_profile_context_search_caps_queries_before_graphiti_call():
    calls = []

    class GraphApi:
        def search(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(edges=[], nodes=[])

    generator = object.__new__(OasisProfileGenerator)
    generator.zep_client = SimpleNamespace(graph=GraphApi())
    generator.graph_id = "graph-id"
    entity = EntityNode(
        uuid="node-id", name="n" * 500, labels=["Entity", "Person"],
        summary="", attributes={},
    )
    generator._search_zep_for_entity(entity)

    assert len(calls) == 2
    assert all(0 < len(call["query"]) <= 400 for call in calls)
    assert all(call["graph_id"] == "graph-id" for call in calls)


def test_entity_context_includes_incoming_and_outgoing_group_edges():
    edges = [
        {"uuid": "edge-in", "name": "WORKS_AT", "fact": "Alice works at Acme",
         "source_node_uuid": "alice", "target_node_uuid": "acme", "attributes": {}},
        {"uuid": "edge-out", "name": "BUILDS", "fact": "Acme builds Product",
         "source_node_uuid": "acme", "target_node_uuid": "product", "attributes": {}},
        {"uuid": "unrelated", "name": "LOCATED_IN", "fact": "OtherCo in Paris",
         "source_node_uuid": "other-company", "target_node_uuid": "paris", "attributes": {}},
    ]
    reader = object.__new__(ZepEntityReader)
    reader.client = SimpleNamespace(graph=SimpleNamespace(node=SimpleNamespace(
        get=lambda **kwargs: SimpleNamespace(
            uuid_="acme", name="Acme", labels=["Company"], summary="", attributes={},
            group_id=kwargs["graph_id"],
        )
    )))
    reader.get_all_edges = lambda _group: edges
    reader.get_all_nodes = lambda _group: [
        {"uuid": "alice", "name": "Alice", "labels": ["Person"], "summary": ""},
        {"uuid": "acme", "name": "Acme", "labels": ["Company"], "summary": ""},
        {"uuid": "product", "name": "Product", "labels": ["Product"], "summary": ""},
    ]

    entity = reader.get_entity_with_context("graph-id", "acme")

    assert entity is not None
    assert len(entity.related_edges) == 2
    assert {edge["direction"] for edge in entity.related_edges} == {"incoming", "outgoing"}
    assert {node["name"] for node in entity.related_nodes} == {"Alice", "Product"}


def test_entity_reader_propagates_graph_store_errors():
    def fail(**_kwargs):
        raise RuntimeError("graph read failed")

    reader = object.__new__(ZepEntityReader)
    reader.client = SimpleNamespace(graph=SimpleNamespace(node=SimpleNamespace(get=fail)))

    with pytest.raises(RuntimeError, match="graph read failed"):
        reader.get_entity_with_context("graph-id", "node-id")


def test_report_tools_propagate_graph_store_errors():
    def fail(**_kwargs):
        raise RuntimeError("graph read failed")

    service = object.__new__(ZepToolsService)
    service.client = SimpleNamespace(graph=SimpleNamespace(node=SimpleNamespace(get=fail)))

    with pytest.raises(RuntimeError, match="graph read failed"):
        service.get_node_detail("node-id", graph_id="graph-id")


def test_graph_builder_writes_chunks_sequentially_and_reports_progress():
    calls = []
    progress = []

    class GraphApi:
        def add(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(uuid=f"graphiti-episode-{len(calls)}")

    builder = object.__new__(GraphBuilderService)
    builder.client = SimpleNamespace(graph=GraphApi())
    submission = builder.add_text_batches(
        "graph-id", ["first", "second", "third"], batch_size=2,
        progress_callback=lambda message, ratio: progress.append((message, ratio)),
    )

    assert [call["data"] for call in calls] == ["first", "second", "third"]
    assert all(call["graph_id"] == "graph-id" for call in calls)
    assert all("uuid" not in call for call in calls)
    assert submission.episode_uuids == [
        "graphiti-episode-1", "graphiti-episode-2", "graphiti-episode-3"
    ]
    assert submission.item_count == 3
    assert progress[-1][1] == 1.0


def test_graphiti_wait_rejects_an_incomplete_episode_submission():
    builder = object.__new__(GraphBuilderService)
    with pytest.raises(RuntimeError, match="count"):
        builder._wait_for_batch(BatchSubmission("op", "op", [], 1))


def test_graph_node_edge_lookup_filters_other_groups():
    edge_a = SimpleNamespace(
        uuid="a", name="A", fact="in group A", source_node_uuid="node",
        target_node_uuid="a-target", attributes={}, created_at=None, valid_at=None,
        invalid_at=None, expired_at=None, episodes=[], group_id="group-a",
    )
    edge_b = SimpleNamespace(
        uuid="b", name="B", fact="in group B", source_node_uuid="node",
        target_node_uuid="b-target", attributes={}, created_at=None, valid_at=None,
        invalid_at=None, expired_at=None, episodes=[], group_id="group-b",
    )

    class Backend:
        graphiti = SimpleNamespace(driver=object())

        def run(self, coroutine):
            coroutine.close()
            return [edge_a, edge_b]

    result = _NodeNamespace(Backend()).get_entity_edges("node", "group-a")
    assert [edge.uuid for edge in result] == ["a"]
