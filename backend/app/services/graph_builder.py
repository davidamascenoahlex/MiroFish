"""
图谱构建服务
接口2：使用Zep API构建Standalone Graph
"""

import hashlib
import uuid
import time
import threading
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass

from .graphiti_adapter import EntityEdgeSourceTarget, EntityModel, EntityText, EdgeModel

from ..config import Config
from ..models.task import TaskManager, TaskStatus
from ..utils.zep_paging import fetch_all_nodes, fetch_all_edges
from ..utils.ontology import (
    MAX_ONTOLOGY_TYPES,
    RESERVED_ONTOLOGY_ATTRIBUTE_NAMES,
    normalize_ontology_attributes,
    normalize_ontology_source_targets,
)
from ..utils.zep import (
    call_graph_read_with_retry,
    get_graphiti_client,
)
from .text_processor import TextProcessor
from ..utils.locale import t, get_locale, set_locale
from ..utils.logger import get_logger

logger = get_logger('mirofish.graph_builder')


@dataclass
class GraphInfo:
    """图谱信息"""
    graph_id: str
    node_count: int
    edge_count: int
    entity_types: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entity_types": self.entity_types,
        }


@dataclass(frozen=True)
class BatchSubmission:
    """Durable identity for one Zep Batch API ingestion operation."""

    batch_id: str
    operation_id: str
    episode_uuids: List[str]
    item_count: int


class GraphBuilderService:
    """
    图谱构建服务
    负责调用Zep API构建知识图谱
    """
    
    EPISODE_ADD_ATTEMPTS = 2
    EPISODE_RETRY_DELAY_SECONDS = 2.0

    def __init__(self, api_key: Optional[str] = None):
        if api_key:
            raise ValueError("Zep Cloud credentials are unsupported; configure Graphiti and Neo4j")
        errors = Config.validate()
        if errors:
            raise ValueError("Invalid Graphiti configuration: " + "; ".join(errors))
        self.client = get_graphiti_client()
        self.task_manager = TaskManager()

    def build_graph_async(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str = "MiroFish Graph",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 350
    ) -> str:
        """
        异步构建图谱
        
        Args:
            text: 输入文本
            ontology: 本体定义（来自接口1的输出）
            graph_name: 图谱名称
            chunk_size: 文本块大小
            chunk_overlap: 块重叠大小
            batch_size: 每批发送的块数量
            
        Returns:
            任务ID
        """
        # 创建任务
        task_id = self.task_manager.create_task(
            task_type="graph_build",
            metadata={
                "graph_name": graph_name,
                "chunk_size": chunk_size,
                "text_length": len(text),
            }
        )
        
        # Capture locale before spawning background thread
        current_locale = get_locale()

        # 在后台线程中执行构建
        thread = threading.Thread(
            target=self._build_graph_worker,
            args=(task_id, text, ontology, graph_name, chunk_size, chunk_overlap, batch_size, current_locale)
        )
        thread.daemon = True
        thread.start()
        
        return task_id
    
    def _build_graph_worker(
        self,
        task_id: str,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int,
        locale: str = 'zh'
    ):
        """图谱构建工作线程"""
        set_locale(locale)
        try:
            self.task_manager.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                progress=5,
                message=t('progress.startBuildingGraph')
            )
            
            # Validate the complete ingestion payload before the first Cloud
            # mutation, including this legacy service entry point.
            chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
            self.validate_batch_chunks(chunks, batch_size=batch_size)
            total_chunks = len(chunks)

            # 1. 创建图谱
            graph_id = self.create_graph(graph_name)
            self.task_manager.update_task(
                task_id,
                progress=10,
                message=t('progress.graphCreated', graphId=graph_id)
            )
            
            # 2. 设置本体
            self.set_ontology(graph_id, ontology)
            self.task_manager.update_task(
                task_id,
                progress=15,
                message=t('progress.ontologySet')
            )
            
            # 3. 文本分块已在 Cloud mutation 前完成并验证
            self.task_manager.update_task(
                task_id,
                progress=20,
                message=t('progress.textSplit', count=total_chunks)
            )
            
            # 4. 分批发送数据
            submission = self.add_text_batches(
                graph_id, chunks, batch_size,
                lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=20 + int(prog * 0.4),  # 20-60%
                    message=msg
                )
            )
            
            # 5. 等待Zep处理完成
            self.task_manager.update_task(
                task_id,
                progress=60,
                message=t('progress.waitingZepProcess')
            )
            
            self._wait_for_batch(
                submission,
                lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=60 + int(prog * 0.3),  # 60-90%
                    message=msg
                )
            )
            
            # 6. 获取图谱信息
            self.task_manager.update_task(
                task_id,
                progress=90,
                message=t('progress.fetchingGraphInfo')
            )
            
            graph_info = self._get_graph_info(graph_id)
            
            # 完成
            self.task_manager.complete_task(task_id, {
                "graph_id": graph_id,
                "graph_info": graph_info.to_dict(),
                "chunks_processed": total_chunks,
            })
            
        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            self.task_manager.fail_task(task_id, error_msg)
    
    def create_graph(
        self,
        name: str,
        *,
        graph_id: str | None = None,
        graph_id_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Allocate a graph partition ID; Graphiti stores it as group_id."""
        graph_id = graph_id or f"mirofish_{uuid.uuid4().hex[:16]}"
        if graph_id_callback:
            graph_id_callback(graph_id)
        self.client.graph.create(graph_id=graph_id, name=name)
        return graph_id

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]):
        """设置图谱本体（公开方法）"""
        import warnings
        from typing import Optional
        from pydantic import Field
        from .graphiti_adapter import EntityModel, EntityText, EdgeModel, EntityEdgeSourceTarget
        
        # 抑制 Pydantic v2 关于 Field(default=None) 的警告
        # 这是 Zep SDK 要求的用法，警告来自动态类创建，可以安全忽略
        warnings.filterwarnings('ignore', category=UserWarning, module='pydantic')
        
        def safe_attr_name(attr_name: str) -> str:
            """将保留名称转换为安全名称"""
            if attr_name.lower() in RESERVED_ONTOLOGY_ATTRIBUTE_NAMES:
                return f"entity_{attr_name}"
            return attr_name
        
        # 动态创建实体类型
        entity_types = {}
        for entity_def in ontology.get("entity_types", [])[:MAX_ONTOLOGY_TYPES]:
            name = entity_def["name"]
            description = entity_def.get("description", f"A {name} entity.")
            
            # 创建属性字典和类型注解（Pydantic v2 需要）
            attrs = {"__doc__": description}
            annotations = {}
            
            for normalized in normalize_ontology_attributes(
                entity_def.get("attributes", [])
            ):
                attr_name = safe_attr_name(normalized["name"])  # 使用安全名称
                attr_desc = normalized["description"]
                # Zep API 需要 Field 的 description，这是必需的
                attrs[attr_name] = Field(description=attr_desc, default=None)
                annotations[attr_name] = Optional[EntityText]  # 类型注解
            
            attrs["__annotations__"] = annotations
            
            # 动态创建类
            entity_class = type(name, (EntityModel,), attrs)
            entity_class.__doc__ = description
            entity_types[name] = entity_class
        
        # 动态创建边类型
        edge_definitions = {}
        for edge_def in ontology.get("edge_types", [])[:MAX_ONTOLOGY_TYPES]:
            name = edge_def["name"]
            description = edge_def.get("description", f"A {name} relationship.")
            
            # 创建属性字典和类型注解
            attrs = {"__doc__": description}
            annotations = {}
            
            for normalized in normalize_ontology_attributes(
                edge_def.get("attributes", [])
            ):
                attr_name = safe_attr_name(normalized["name"])  # 使用安全名称
                attr_desc = normalized["description"]
                # Zep API 需要 Field 的 description，这是必需的
                attrs[attr_name] = Field(description=attr_desc, default=None)
                annotations[attr_name] = Optional[str]  # 边属性用str类型
            
            attrs["__annotations__"] = annotations
            
            # 动态创建类
            class_name = ''.join(word.capitalize() for word in name.split('_'))
            edge_class = type(class_name, (EdgeModel,), attrs)
            edge_class.__doc__ = description
            
            # 构建source_targets
            source_targets = []
            for st in normalize_ontology_source_targets(
                edge_def.get("source_targets", [])
            ):
                source_targets.append(
                    EntityEdgeSourceTarget(
                        source=st.get("source", "Entity"),
                        target=st.get("target", "Entity")
                    )
                )
            
            if source_targets:
                edge_definitions[name] = (edge_class, source_targets)
        
        # 调用Zep API设置本体
        if entity_types or edge_definitions:
            self.client.graph.set_ontology(
                graph_ids=[graph_id],
                # Zep iterates entities.items(), so edge-only ontologies must
                # pass an empty dictionary rather than None.
                entities=entity_types,
                edges=edge_definitions if edge_definitions else None,
            )
    
    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 350,
        progress_callback: Optional[Callable] = None,
        batch_created_callback: Optional[Callable[[str | None, str], None]] = None,
        *,
        resume: bool = False,
    ) -> BatchSubmission:
        """Add chunks sequentially as Graphiti episodes, without replaying writes.

        With resume=True, chunk episodes already stored with identical content
        are reused, so a failed build continues where it stopped.
        """
        if not graph_id:
            raise ValueError("graph_id is required")
        self.validate_batch_chunks(chunks, batch_size=batch_size)
        operation_id = self.build_operation_id(graph_id, chunks)
        if batch_created_callback:
            batch_created_callback(operation_id, operation_id)

        stored = self._stored_chunk_episodes(graph_id) if resume else {}
        reused = 0
        episode_uuids: List[str] = []
        total_batches = (len(chunks) + batch_size - 1) // batch_size
        for index, chunk in enumerate(chunks):
            batch_num = index // batch_size + 1
            name = f"chunk-{index + 1}"
            episode_uuid = self._stored_episode_uuid(stored, name, chunk)
            if episode_uuid:
                reused += 1
            else:
                episode_uuid = self._add_chunk_episode(graph_id, name, chunk)
            episode_uuids.append(episode_uuid)
            if progress_callback:
                progress_callback(
                    t(
                        'progress.sendingBatch',
                        current=batch_num,
                        total=total_batches,
                        chunks=min(batch_size, len(chunks) - (batch_num - 1) * batch_size),
                    ),
                    (index + 1) / len(chunks),
                )

        if resume:
            logger.info(
                f"Resumed graph {graph_id}: reused {reused}/{len(chunks)} chunk episodes"
            )
        return BatchSubmission(
            batch_id=operation_id,
            operation_id=operation_id,
            episode_uuids=episode_uuids,
            item_count=len(chunks),
        )

    def _stored_chunk_episodes(self, graph_id: str) -> Dict[str, List[Dict[str, Any]]]:
        stored: Dict[str, List[Dict[str, Any]]] = {}
        for episode in self.client.graph.list_episodes(graph_id=graph_id):
            stored.setdefault(episode.get("name"), []).append(episode)
        return stored

    @staticmethod
    def _stored_episode_uuid(
        stored: Dict[str, List[Dict[str, Any]]], name: str, chunk: str
    ) -> Optional[str]:
        episodes = stored.get(name)
        if not episodes:
            return None
        for episode in episodes:
            if episode.get("content") == chunk and episode.get("uuid"):
                return episode["uuid"]
        raise RuntimeError(
            f"Stored episode {name} does not match the current text; "
            "rebuild the graph with force=true"
        )

    def _add_chunk_episode(self, graph_id: str, name: str, chunk: str) -> str:
        attempts = max(1, int(self.EPISODE_ADD_ATTEMPTS))
        attempt = 1
        while True:
            try:
                episode = self.client.graph.add(
                    graph_id=graph_id,
                    data=chunk,
                    type="text",
                    name=name,
                    source_description="MiroFish source document chunk",
                )
            except Exception as error:
                if attempt >= attempts:
                    raise
                logger.warning(
                    f"Graphiti failed to add {name} to {graph_id} "
                    f"(attempt {attempt}/{attempts}): {error}"
                )
                # The write may have committed before the error surfaced.
                stored_uuid = self._stored_episode_uuid(
                    self._stored_chunk_episodes(graph_id), name, chunk
                )
                if stored_uuid:
                    return stored_uuid
                time.sleep(self.EPISODE_RETRY_DELAY_SECONDS)
                attempt += 1
                continue
            episode_uuid = getattr(episode, "uuid_", None) or getattr(
                episode, "uuid", None
            )
            if not isinstance(episode_uuid, str) or not episode_uuid.strip():
                raise RuntimeError("Graphiti graph.add returned no episode UUID")
            return episode_uuid

    @staticmethod
    def build_operation_id(graph_id: str, chunks: List[str]) -> str:
        payload_hash = hashlib.sha256("\0".join(chunks).encode("utf-8")).hexdigest()
        return hashlib.sha256(f"{graph_id}:{payload_hash}".encode("utf-8")).hexdigest()

    @staticmethod
    def validate_batch_chunks(chunks: List[str], *, batch_size: int = 350) -> None:
        if not chunks:
            raise ValueError("At least one text chunk is required")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, str) or not chunk.strip():
                raise ValueError(f"Text chunk at index {index} must be a non-empty string")

    def _wait_for_batch(
        self,
        submission: BatchSubmission,
        progress_callback: Optional[Callable] = None,
        timeout: int | None = None,
    ) -> List[str]:
        """Graphiti writes complete before add returns; no remote batch polling is needed."""
        if len(submission.episode_uuids) != submission.item_count:
            raise RuntimeError("Graphiti episode submission count does not match the input")
        if progress_callback:
            progress_callback(
                t(
                    'progress.processingComplete',
                    completed=submission.item_count,
                    total=submission.item_count,
                ),
                1.0,
            )
        return submission.episode_uuids

    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """获取图谱信息"""
        # 获取节点（分页）
        nodes = fetch_all_nodes(self.client, graph_id)

        # 获取边（分页）
        edges = fetch_all_edges(self.client, graph_id)

        # 统计实体类型
        entity_types = set()
        for node in nodes:
            if node.labels:
                for label in node.labels:
                    if label not in ["Entity", "Node"]:
                        entity_types.add(label)

        return GraphInfo(
            graph_id=graph_id,
            node_count=len(nodes),
            edge_count=len(edges),
            entity_types=list(entity_types)
        )
    
    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """
        获取完整图谱数据（包含详细信息）
        
        Args:
            graph_id: 图谱ID
            
        Returns:
            包含nodes和edges的字典，包括时间信息、属性等详细数据
        """
        nodes = fetch_all_nodes(self.client, graph_id)
        edges = fetch_all_edges(self.client, graph_id)

        # 创建节点映射用于获取节点名称
        node_map = {}
        for node in nodes:
            node_map[node.uuid_] = node.name or ""
        
        nodes_data = []
        for node in nodes:
            # 获取创建时间
            created_at = getattr(node, 'created_at', None)
            if created_at:
                created_at = str(created_at)
            
            nodes_data.append({
                "uuid": node.uuid_,
                "name": node.name,
                "labels": node.labels or [],
                "summary": node.summary or "",
                "attributes": node.attributes or {},
                "created_at": created_at,
            })
        
        edges_data = []
        for edge in edges:
            # 获取时间信息
            created_at = getattr(edge, 'created_at', None)
            valid_at = getattr(edge, 'valid_at', None)
            invalid_at = getattr(edge, 'invalid_at', None)
            expired_at = getattr(edge, 'expired_at', None)
            
            # 获取 episodes
            episodes = getattr(edge, 'episodes', None) or getattr(edge, 'episode_ids', None)
            if episodes and not isinstance(episodes, list):
                episodes = [str(episodes)]
            elif episodes:
                episodes = [str(e) for e in episodes]
            
            # 获取 fact_type
            fact_type = getattr(edge, 'fact_type', None) or edge.name or ""
            
            edges_data.append({
                "uuid": edge.uuid_,
                "name": edge.name or "",
                "fact": edge.fact or "",
                "fact_type": fact_type,
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "source_node_name": node_map.get(edge.source_node_uuid, ""),
                "target_node_name": node_map.get(edge.target_node_uuid, ""),
                "attributes": edge.attributes or {},
                "created_at": str(created_at) if created_at else None,
                "valid_at": str(valid_at) if valid_at else None,
                "invalid_at": str(invalid_at) if invalid_at else None,
                "expired_at": str(expired_at) if expired_at else None,
                "episodes": episodes or [],
            })
        
        return {
            "graph_id": graph_id,
            "nodes": nodes_data,
            "edges": edges_data,
            "node_count": len(nodes_data),
            "edge_count": len(edges_data),
        }
    
    def delete_graph(self, graph_id: str):
        """删除图谱"""
        self.client.graph.delete(graph_id=graph_id)
