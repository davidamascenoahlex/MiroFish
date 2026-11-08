from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, get_args

from pydantic import BaseModel

from ..config import Config

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """Requested graph entity does not exist in the requested group."""


class InternalServerError(Exception):
    """Compatibility exception for transient graph-store failures."""


class EntityEdgeSourceTarget:
    def __init__(self, source: str | None = None, target: str | None = None):
        self.source = source
        self.target = target


class EntityModel(BaseModel):
    pass


class EdgeModel(BaseModel):
    pass


EntityText = str


_NEO4J_SCALAR_TYPES = (str, int, float, bool)


def _field_accepts_text(field_info: Any) -> bool:
    annotation = field_info.annotation
    return annotation is str or str in get_args(annotation)


def _attribute_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            item_text = _attribute_text(item)
            if item_text:
                parts.append(f"{key}: {item_text}")
        text = "; ".join(parts)
    elif isinstance(value, (list, tuple)):
        text = ", ".join(item for item in map(_attribute_text, value) if item)
    else:
        text = str(value).strip()
    return text or None


def _attribute_value(value: Any, accepts_text: bool) -> Any:
    if accepts_text:
        return _attribute_text(value)
    if isinstance(value, _NEO4J_SCALAR_TYPES):
        return value
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, _NEO4J_SCALAR_TYPES) for item in value)
    ):
        return value
    return None


def normalize_ontology_attribute_response(
    response: Any, model: type[BaseModel]
) -> dict[str, Any]:
    """Keep only declared ontology attributes, as flat values Neo4j can store.

    Graphiti persists attribute-extraction output almost verbatim. In
    json_object mode LLMs sometimes wrap it in an object keyed by the type
    name, which Neo4j rejects as a Map property and fails the whole episode.
    """
    if not isinstance(response, dict):
        return {}
    fields = model.model_fields
    wrapped = [
        value
        for key, value in response.items()
        if key not in fields
        and isinstance(value, dict)
        and any(name in fields for name in value)
    ]
    candidate = dict(wrapped[0]) if len(wrapped) == 1 else {}
    candidate.update(
        (key, value)
        for key, value in response.items()
        if key in fields and value is not None
    )
    normalized: dict[str, Any] = {}
    for name, value in candidate.items():
        field_info = fields.get(name)
        if field_info is None:
            continue
        value = _attribute_value(value, _field_accepts_text(field_info))
        if value is not None:
            normalized[name] = value
    return normalized


@lru_cache(maxsize=1)
def _ontology_safe_llm_client_class():
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    class OntologySafeOpenAIGenericClient(OpenAIGenericClient):
        async def generate_response(
            self, messages, response_model=None, *args, attribute_extraction=False, **kwargs
        ):
            response = await super().generate_response(
                messages,
                response_model,
                *args,
                attribute_extraction=attribute_extraction,
                **kwargs,
            )
            if (
                attribute_extraction
                and isinstance(response_model, type)
                and issubclass(response_model, (EntityModel, EdgeModel))
            ):
                return normalize_ontology_attribute_response(response, response_model)
            return response

    return OntologySafeOpenAIGenericClient


class _GraphitiBackend:
    def __init__(self) -> None:
        errors = Config.validate()
        if errors:
            raise ValueError("Invalid Graphiti configuration: " + "; ".join(errors))
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve_loop, name="mirofish-graphiti", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        self._ontology_lock = threading.Lock()
        self._ontologies: dict[str, tuple[dict, dict, dict]] = {}
        self.graphiti = self.run(self._build_graphiti())
        logger.info("Graphiti connected to configured Neo4j store")

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result()

    async def _build_graphiti(self):
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig

        llm_config = LLMConfig(
            api_key=Config.LLM_API_KEY,
            model=Config.LLM_MODEL_NAME,
            base_url=Config.LLM_BASE_URL,
        )
        llm_client = _ontology_safe_llm_client_class()(
            config=llm_config,
            structured_output_mode=Config.GRAPHITI_STRUCTURED_OUTPUT_MODE,
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=Config.EMBEDDING_API_KEY,
                base_url=Config.EMBEDDING_BASE_URL,
                embedding_model=Config.EMBEDDING_MODEL_NAME,
                embedding_dim=int(Config.EMBEDDING_DIM),
            )
        )
        cross_encoder = OpenAIRerankerClient(config=llm_config)
        graphiti = Graphiti(
            uri=Config.NEO4J_URI,
            user=Config.NEO4J_USER,
            password=Config.NEO4J_PASSWORD,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=cross_encoder,
        )
        await graphiti.build_indices_and_constraints()
        return graphiti

    def set_ontology(
        self, graph_id: str, entity_types: dict, edge_types: dict, edge_type_map: dict
    ) -> None:
        with self._ontology_lock:
            self._ontologies[graph_id] = (entity_types, edge_types, edge_type_map)

    def get_ontology(self, graph_id: str) -> tuple[dict | None, dict | None, dict | None]:
        with self._ontology_lock:
            ontology = self._ontologies.get(graph_id)
        if ontology is None:
            return None, None, None
        entity_types, edge_types, edge_type_map = ontology
        return entity_types or None, edge_types or None, edge_type_map or None

    async def delete_group(self, graph_id: str) -> None:
        await self.graphiti.driver.execute_query(
            "MATCH (n) "
            "WHERE n.group_id = $group_id "
            "AND any(label IN labels(n) WHERE label IN $managed_labels) "
            "DETACH DELETE n",
            group_id=graph_id,
            managed_labels=["Entity", "Episodic", "Community"],
        )

    def close(self) -> None:
        if self._loop.is_running():
            self.run(self.graphiti.close())
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)


_backend: _GraphitiBackend | None = None
_backend_lock = threading.Lock()


def _get_backend() -> _GraphitiBackend:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _GraphitiBackend()
                atexit.register(_backend.close)
    return _backend


def _require_group_id(graph_id: str | None) -> str:
    if not isinstance(graph_id, str) or not graph_id.strip():
        raise ValueError("graph_id is required")
    return graph_id


class _Node:
    __slots__ = ("uuid_", "uuid", "name", "labels", "summary", "attributes", "created_at", "group_id")

    def __init__(self, node: Any):
        self.uuid_ = node.uuid
        self.uuid = node.uuid
        self.name = node.name or ""
        self.labels = list(node.labels or [])
        self.summary = node.summary or ""
        self.attributes = dict(node.attributes or {})
        self.created_at = node.created_at
        self.group_id = node.group_id


class _Edge:
    __slots__ = (
        "uuid_", "uuid", "name", "fact", "source_node_uuid", "target_node_uuid",
        "attributes", "created_at", "valid_at", "invalid_at", "expired_at",
        "episodes", "episode_ids", "fact_type", "group_id",
    )

    def __init__(self, edge: Any):
        self.uuid_ = edge.uuid
        self.uuid = edge.uuid
        self.name = edge.name or ""
        self.fact = edge.fact or ""
        self.source_node_uuid = edge.source_node_uuid
        self.target_node_uuid = edge.target_node_uuid
        self.attributes = dict(edge.attributes or {})
        self.created_at = edge.created_at
        self.valid_at = edge.valid_at
        self.invalid_at = edge.invalid_at
        self.expired_at = edge.expired_at
        self.episodes = list(edge.episodes or [])
        self.episode_ids = self.episodes
        self.fact_type = self.name
        self.group_id = edge.group_id


class _SearchResult:
    def __init__(self, edges: list[_Edge], nodes: list[_Node]):
        self.edges = edges
        self.nodes = nodes


class _NodeNamespace:
    def __init__(self, backend: _GraphitiBackend):
        self._backend = backend

    def get(self, uuid_: str | None = None, graph_id: str | None = None, **_: Any):
        if not uuid_:
            return None
        graph_id = _require_group_id(graph_id)
        from graphiti_core.errors import NodeNotFoundError
        from graphiti_core.nodes import EntityNode

        try:
            node = self._backend.run(
                EntityNode.get_by_uuid(self._backend.graphiti.driver, uuid_)
            )
        except NodeNotFoundError as error:
            raise NotFoundError(uuid_) from error
        if node.group_id != graph_id:
            return None
        return _Node(node)

    def get_by_graph_id(
        self, graph_id: str, limit: int | None = None, uuid_cursor: str | None = None, **_: Any
    ) -> list[_Node]:
        graph_id = _require_group_id(graph_id)
        from graphiti_core.nodes import EntityNode

        nodes = self._backend.run(
            EntityNode.get_by_group_ids(
                self._backend.graphiti.driver,
                [graph_id],
                limit=limit,
                uuid_cursor=uuid_cursor,
            )
        )
        return [_Node(node) for node in nodes]

    def get_entity_edges(
        self, node_uuid: str | None = None, graph_id: str | None = None, **_: Any
    ) -> list[_Edge]:
        if not node_uuid:
            return []
        graph_id = _require_group_id(graph_id)
        from graphiti_core.edges import EntityEdge

        edges = self._backend.run(
            EntityEdge.get_by_node_uuid(self._backend.graphiti.driver, node_uuid)
        )
        return [_Edge(edge) for edge in edges if edge.group_id == graph_id]


class _EdgeNamespace:
    def __init__(self, backend: _GraphitiBackend):
        self._backend = backend

    def get_by_graph_id(
        self, graph_id: str, limit: int | None = None, uuid_cursor: str | None = None, **_: Any
    ) -> list[_Edge]:
        graph_id = _require_group_id(graph_id)
        from graphiti_core.edges import EntityEdge
        from graphiti_core.errors import GroupsEdgesNotFoundError

        try:
            edges = self._backend.run(
                EntityEdge.get_by_group_ids(
                    self._backend.graphiti.driver,
                    [graph_id],
                    limit=limit,
                    uuid_cursor=uuid_cursor,
                )
            )
        except GroupsEdgesNotFoundError:
            return []
        return [_Edge(edge) for edge in edges]


class _Episode:
    __slots__ = ("uuid_", "uuid", "group_id", "processed")

    def __init__(self, episode: Any):
        self.uuid_ = episode.uuid
        self.uuid = episode.uuid
        self.group_id = episode.group_id
        # Graphiti commits episodes with their extracted data before add returns.
        self.processed = True


class _EpisodeNamespace:
    def __init__(self, backend: _GraphitiBackend):
        self._backend = backend

    def get(
        self, uuid_: str | None = None, graph_id: str | None = None, **_: Any
    ) -> _Episode:
        graph_id = _require_group_id(graph_id)
        if not isinstance(uuid_, str) or not uuid_.strip():
            raise ValueError("episode UUID is required")
        from graphiti_core.errors import NodeNotFoundError
        from graphiti_core.nodes import EpisodicNode

        try:
            episode = self._backend.run(
                EpisodicNode.get_by_uuid(self._backend.graphiti.driver, uuid_)
            )
        except NodeNotFoundError as error:
            raise NotFoundError(uuid_) from error
        if episode.group_id != graph_id:
            raise NotFoundError(uuid_)
        return _Episode(episode)


class _GraphNamespace:
    def __init__(self, backend: _GraphitiBackend):
        self._backend = backend
        self.node = _NodeNamespace(backend)
        self.edge = _EdgeNamespace(backend)
        self.episode = _EpisodeNamespace(backend)

    def create(self, graph_id: str | None = None, **_: Any) -> None:
        _require_group_id(graph_id)

    def set_ontology(
        self,
        graph_ids: list[str] | None = None,
        entities: dict | None = None,
        edges: dict | None = None,
        **_: Any,
    ) -> None:
        entity_types = dict(entities or {})
        edge_types: dict[str, Any] = {}
        edge_type_map: dict[tuple[str, str], list[str]] = {}
        for name, definition in (edges or {}).items():
            edge_type, source_targets = definition if isinstance(definition, (tuple, list)) else (definition, [])
            edge_types[name] = edge_type
            for source_target in source_targets:
                key = (
                    source_target.source or "Entity",
                    source_target.target or "Entity",
                )
                edge_type_map.setdefault(key, []).append(name)
            if not source_targets:
                edge_type_map.setdefault(("Entity", "Entity"), []).append(name)
        for graph_id in graph_ids or []:
            self._backend.set_ontology(
                _require_group_id(graph_id), entity_types, edge_types, edge_type_map
            )

    def delete(self, graph_id: str | None = None, **_: Any) -> None:
        graph_id = _require_group_id(graph_id)
        self._backend.run(self._backend.delete_group(graph_id))

    def list_episodes(self, graph_id: str | None = None, **_: Any) -> list[dict[str, Any]]:
        graph_id = _require_group_id(graph_id)
        result = self._backend.run(
            self._backend.graphiti.driver.execute_query(
                "MATCH (e:Episodic {group_id: $group_id}) "
                "RETURN e.uuid AS uuid, e.name AS name, e.content AS content",
                group_id=graph_id,
            )
        )
        return [
            {"uuid": record["uuid"], "name": record["name"], "content": record["content"]}
            for record in result.records
        ]

    def add(
        self,
        graph_id: str | None = None,
        data: Any = None,
        type: str = "text",
        name: str = "episode",
        source_description: str = "MiroFish",
        uuid: str | None = None,
        **_: Any,
    ):
        graph_id = _require_group_id(graph_id)
        if not isinstance(data, str) or not data.strip():
            raise ValueError("episode data must be a non-empty string")
        from graphiti_core.nodes import EpisodeType

        entity_types, edge_types, edge_type_map = self._backend.get_ontology(graph_id)
        result = self._backend.run(
            self._backend.graphiti.add_episode(
                name=name,
                episode_body=data,
                source_description=source_description,
                reference_time=datetime.now(timezone.utc),
                source=EpisodeType.text,
                group_id=graph_id,
                uuid=uuid,
                entity_types=entity_types,
                edge_types=edge_types,
                edge_type_map=edge_type_map,
            )
        )
        return result.episode

    def search(
        self,
        graph_id: str | None = None,
        query: str | None = None,
        limit: int = 10,
        scope: str = "edges",
        **_: Any,
    ) -> _SearchResult:
        graph_id = _require_group_id(graph_id)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be a non-empty string")
        try:
            result_limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError) as error:
            raise ValueError("search limit must be an integer") from error

        if scope == "edges":
            edges = self._backend.run(
                self._backend.graphiti.search(
                    query=query, group_ids=[graph_id], num_results=result_limit
                )
            )
            return _SearchResult([_Edge(edge) for edge in edges], [])

        from graphiti_core.search.search_config import SearchConfig
        from graphiti_core.search.search_config_recipes import (
            COMBINED_HYBRID_SEARCH_RRF,
            NODE_HYBRID_SEARCH_RRF,
        )

        if scope == "nodes":
            config = NODE_HYBRID_SEARCH_RRF.model_copy(deep=True)
            config.limit = result_limit
        else:
            config = COMBINED_HYBRID_SEARCH_RRF.model_copy(deep=True)
            config.limit = result_limit
        results = self._backend.run(
            self._backend.graphiti.search_(
                query=query, config=config, group_ids=[graph_id]
            )
        )
        return _SearchResult(
            [_Edge(edge) for edge in results.edges or []],
            [_Node(node) for node in results.nodes or []],
        )


class GraphitiClient:
    """Synchronous application adapter over the async Graphiti client."""

    def __init__(self):
        self.graph = _GraphNamespace(_get_backend())
