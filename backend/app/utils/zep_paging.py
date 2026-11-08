"""Cursor pagination for Graphiti graph groups."""

from __future__ import annotations

from typing import Any

from .logger import get_logger
from .zep import call_graph_read_with_retry

logger = get_logger("mirofish.graph_paging")
_DEFAULT_PAGE_SIZE = 100
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 2.0


def _fetch_all(method: Any, graph_id: str, *, item_name: str, page_size: int,
               max_items: int | None, max_retries: int, retry_delay: float) -> list[Any]:
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    if max_items is not None and max_items < 1:
        raise ValueError("max_items must be at least 1 when provided")

    items: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = call_graph_read_with_retry(
            lambda: method(graph_id, limit=page_size, uuid_cursor=cursor),
            operation_name=f"fetch {item_name} (graph={graph_id})",
            max_attempts=max_retries,
            initial_delay=retry_delay,
        )
        if not isinstance(page, list):
            raise TypeError(f"Graphiti {item_name} page must be a list")
        items.extend(page)
        if max_items is not None and len(items) >= max_items:
            items = items[:max_items]
            logger.warning("Graphiti %s pagination reached max_items=%s for %s", item_name, max_items, graph_id)
            break
        if len(page) < page_size:
            break
        next_cursor = getattr(page[-1], "uuid_", None) or getattr(page[-1], "uuid", None)
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            raise RuntimeError(f"Graphiti {item_name} pagination cursor did not advance for graph {graph_id}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return items


def fetch_all_nodes(client: Any, graph_id: str, page_size: int = _DEFAULT_PAGE_SIZE,
                    max_items: int | None = None, max_retries: int = _DEFAULT_MAX_RETRIES,
                    retry_delay: float = _DEFAULT_RETRY_DELAY) -> list[Any]:
    return _fetch_all(client.graph.node.get_by_graph_id, graph_id, item_name="nodes",
                      page_size=page_size, max_items=max_items, max_retries=max_retries,
                      retry_delay=retry_delay)


def fetch_all_edges(client: Any, graph_id: str, page_size: int = _DEFAULT_PAGE_SIZE,
                    max_retries: int = _DEFAULT_MAX_RETRIES, retry_delay: float = _DEFAULT_RETRY_DELAY,
                    max_items: int | None = None) -> list[Any]:
    return _fetch_all(client.graph.edge.get_by_graph_id, graph_id, item_name="edges",
                      page_size=page_size, max_items=max_items, max_retries=max_retries,
                      retry_delay=retry_delay)
