"""Graph-store client and bounded retry helpers retained for existing services."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

import httpx
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from ..services.graphiti_adapter import GraphitiClient

logger = logging.getLogger("mirofish.graph_store")
T = TypeVar("T")

GRAPH_INGESTION_WAIT_TIMEOUT_SECONDS = 600
GRAPH_HTTP_REQUEST_TIMEOUT_SECONDS = 120
MAX_GRAPH_SEARCH_QUERY_CHARS = 400
MAX_GRAPH_SEARCH_RESULTS = 50


def normalize_graph_search_query(query: Any) -> str:
    if not isinstance(query, str):
        raise ValueError("Graph search query must be a string")
    normalized = query.strip()
    if not normalized:
        raise ValueError("Graph search query must not be empty")
    return normalized[:MAX_GRAPH_SEARCH_QUERY_CHARS]


def normalize_graph_search_limit(limit: Any) -> int:
    try:
        normalized = int(limit)
    except (TypeError, ValueError) as error:
        raise ValueError("Graph search limit must be an integer") from error
    if normalized < 1:
        raise ValueError("Graph search limit must be at least 1")
    return min(normalized, MAX_GRAPH_SEARCH_RESULTS)


def get_graphiti_client(api_key: str | None = None, timeout: float | None = None) -> GraphitiClient:
    if api_key:
        raise ValueError("Zep Cloud credentials are unsupported; configure Graphiti and Neo4j")
    if timeout is not None and timeout <= 0:
        raise ValueError("Graph request timeout must be greater than 0")
    return GraphitiClient()


def clear_graphiti_client_cache() -> None:
    """Compatibility no-op; the Graphiti backend is a process-wide singleton."""


def is_retryable_graph_error(error: BaseException) -> bool:
    return isinstance(
        error,
        (
            httpx.TimeoutException,
            httpx.TransportError,
            ServiceUnavailable,
            SessionExpired,
            TransientError,
            ConnectionError,
            TimeoutError,
            OSError,
        ),
    )


def call_graph_read_with_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
    max_attempts: int = 3,
    initial_delay: float = 2.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            if attempt == max_attempts or not is_retryable_graph_error(error):
                raise
            delay = min(initial_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning(
                "Graph %s attempt %s/%s failed (%s); retrying in %.1fs",
                operation_name,
                attempt,
                max_attempts,
                type(error).__name__,
                delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")
