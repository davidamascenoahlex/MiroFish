from types import SimpleNamespace

from app.services.graphiti_adapter import _NodeNamespace


def test_node_edge_lookup_filters_edges_by_group_id():
    edges = [
        SimpleNamespace(uuid="edge-a", name="A", fact="", source_node_uuid="node",
                        target_node_uuid="a", attributes={}, created_at=None,
                        valid_at=None, invalid_at=None, expired_at=None, episodes=[],
                        group_id="group-a"),
        SimpleNamespace(uuid="edge-b", name="B", fact="", source_node_uuid="node",
                        target_node_uuid="b", attributes={}, created_at=None,
                        valid_at=None, invalid_at=None, expired_at=None, episodes=[],
                        group_id="group-b"),
    ]

    class Backend:
        graphiti = SimpleNamespace(driver=object())

        def run(self, coroutine):
            coroutine.close()
            return edges

    result = _NodeNamespace(Backend()).get_entity_edges("node", "group-a")
    assert [edge.uuid for edge in result] == ["edge-a"]
