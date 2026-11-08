from types import SimpleNamespace

import pytest

from app.utils import zep_paging


def _client(method):
    return SimpleNamespace(graph=SimpleNamespace(edge=SimpleNamespace(get_by_graph_id=method)))


def test_edge_cap_stops_pagination_and_truncates_results():
    calls = []
    pages = [
        [SimpleNamespace(uuid_="e1"), SimpleNamespace(uuid_="e2")],
        [SimpleNamespace(uuid_="e3"), SimpleNamespace(uuid_="e4")],
    ]

    def get_edges(graph_id, **kwargs):
        calls.append((graph_id, kwargs))
        return pages[len(calls) - 1]

    result = zep_paging.fetch_all_edges(_client(get_edges), "group", page_size=2, max_items=3)

    assert [edge.uuid_ for edge in result] == ["e1", "e2", "e3"]
    assert len(calls) == 2
    assert calls[1] == ("group", {"limit": 2, "uuid_cursor": "e2"})


def test_pagination_stops_after_short_page():
    calls = []

    def get_edges(_graph_id, **_kwargs):
        calls.append(True)
        return [SimpleNamespace(uuid_="e1")]

    assert len(zep_paging.fetch_all_edges(_client(get_edges), "group", page_size=2)) == 1
    assert len(calls) == 1


def test_pagination_rejects_a_repeated_cursor():
    page = [SimpleNamespace(uuid_="e1"), SimpleNamespace(uuid_="e2")]
    result = zep_paging._fetch_all

    with pytest.raises(RuntimeError, match="cursor did not advance"):
        result(lambda *_args, **_kwargs: page, "group", item_name="edges", page_size=2,
               max_items=None, max_retries=1, retry_delay=0)


def test_pagination_validates_page_size():
    with pytest.raises(ValueError, match="page_size"):
        zep_paging.fetch_all_edges(_client(lambda *_args, **_kwargs: []), "group", page_size=0)
