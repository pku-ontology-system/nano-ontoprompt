"""ChromaDB 向量数据库服务"""
from __future__ import annotations
import hashlib
import json
import logging
import time
from typing import Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    import chromadb
except ImportError:
    chromadb = None

# 连接失败后多少秒内不再重试 (heartbeat 超时会阻塞每个请求数秒)
_RETRY_INTERVAL = 60.0
_MAX_SEARCH_QUERY_LENGTH = 1000
_MAX_SEARCH_RESULTS = 100
MAX_TEMPORAL_INDEX_FACTS = 25000
_MAX_TEMPORAL_INDEX_FACTS = MAX_TEMPORAL_INDEX_FACTS
TEMPORAL_INDEX_BATCH_SIZE = 500
_TEMPORAL_INDEX_BATCH_SIZE = TEMPORAL_INDEX_BATCH_SIZE


def temporal_replay_index_source_revision(replay: Any) -> str:
    """Digest every replay/schema pin that determines a temporal candidate index."""
    payload = {
        "replay_id": str(getattr(replay, "id", "") or ""),
        "updated_at": replay.updated_at.isoformat() if getattr(replay, "updated_at", None) else None,
        "committed_events": int(getattr(replay, "committed_events", 0) or 0),
        "watermark_ordinal": str(getattr(replay, "watermark_ordinal", None)) if getattr(replay, "watermark_ordinal", None) is not None else None,
        "watermark_sequence": getattr(replay, "watermark_sequence", None),
        "published_snapshot_id": getattr(replay, "published_snapshot_id", None),
        "schema_revision_id": getattr(replay, "schema_revision_id", None),
        "dataset_version_id": getattr(replay, "dataset_version_id", None),
        "source_id": getattr(replay, "source_id", None),
        "time_kind": getattr(replay, "time_kind", None),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ChromaService:
    """ChromaDB 连接与向量存储/检索服务

    使用默认配置时全进程共享同一个 client; 连接失败后 60 秒内直接判定不可用,
    避免每次实例化都触发 heartbeat 超时 (此前 integrations/status 每次卡 ~6 秒)。
    """

    _shared_client = None
    _shared_unavailable_until: float = 0.0

    def __init__(self, host: str | None = None, port: int | None = None):
        from app.config import settings
        self._is_default = host is None and port is None
        self._host = host or settings.chroma_host
        self._port = port or settings.chroma_port
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self):
        cls = ChromaService
        if self._is_default:
            if cls._shared_client is not None:
                self._client = cls._shared_client
                self._available = True
                return
            if time.monotonic() < cls._shared_unavailable_until:
                return
        try:
            if chromadb is None:
                raise ImportError("chromadb not installed")
            client = chromadb.HttpClient(host=self._host, port=self._port)
            client.heartbeat()
            self._client = client
            self._available = True
            if self._is_default:
                cls._shared_client = client
            logger.info("ChromaDB connected")
        except Exception as e:
            logger.warning(f"ChromaDB unavailable: {e}")
            self._available = False
            if self._is_default:
                cls._shared_unavailable_until = time.monotonic() + _RETRY_INTERVAL

    @property
    def available(self) -> bool:
        return self._available

    # ── 集合管理 ────────────────────────────────────────────────────

    def get_or_create_collection(self, name: str) -> Any | None:
        """返回或创建集合 (使用余弦距离)"""
        if not self._available:
            return None
        try:
            return self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            logger.warning(f"ChromaDB collection error: {e}")
            return None

    def delete_collection(self, name: str) -> bool:
        if not self._available:
            return False
        try:
            self._client.delete_collection(name)
            return True
        except Exception:
            return False

    # ── 写入 ────────────────────────────────────────────────────────

    def upsert_entities(self, ontology_id: str, entities: list[dict]) -> int:
        """将实体 upsert 到集合 (自动文本嵌入)"""
        if not self._available or not entities:
            return 0
        collection = self.get_or_create_collection(f"ontology_{ontology_id}")
        if not collection:
            return 0

        ids = [e.get("id", str(i)) for i, e in enumerate(entities)]
        documents = [self._entity_to_text(e) for e in entities]
        metadatas = [
            {
                "entity_type": str(e.get("type", "")),
                "name_cn": str(e.get("name_cn", "")),
                "name_en": str(e.get("name_en", "")),
                "confidence": float(e.get("confidence", 0.0)),
            }
            for e in entities
        ]

        try:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            return len(ids)
        except Exception as e:
            logger.warning(f"ChromaDB upsert error: {e}")
            return 0

    def delete_entities(self, ontology_id: str, entity_ids: list[str]) -> bool:
        """删除指定 ID 的实体"""
        if not self._available:
            return False
        collection = self.get_or_create_collection(f"ontology_{ontology_id}")
        if not collection:
            return False
        try:
            collection.delete(ids=entity_ids)
            return True
        except Exception:
            return False

    # ── 检索 ────────────────────────────────────────────────────────

    def semantic_search(
        self,
        ontology_id: str,
        query: str,
        n_results: int = 10,
        entity_type: str | None = None,
    ) -> list[dict]:
        """语义检索 — ChromaDB 向量相似度搜索"""
        query = str(query or "").strip()[:_MAX_SEARCH_QUERY_LENGTH]
        n_results = max(1, min(int(n_results), _MAX_SEARCH_RESULTS))
        if not query:
            return []
        if not self._available:
            return []
        collection = self.get_or_create_collection(f"ontology_{ontology_id}")
        if not collection:
            return []

        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if entity_type:
            kwargs["where"] = {"entity_type": entity_type}

        try:
            results = collection.query(**kwargs)
            hits = []
            ids = results.get("ids", [[]])[0]
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            for i, eid in enumerate(ids):
                hits.append({
                    "id": eid,
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "score": 1.0 - (distances[i] if i < len(distances) else 1.0),
                })
            return hits
        except Exception as e:
            logger.warning(f"ChromaDB search error: {e}")
            return []

    @staticmethod
    def _temporal_collection_name(replay_id: str, source_revision: str | None = None) -> str:
        identity = str(replay_id) if source_revision is None else f"{replay_id}:{source_revision}"
        replay_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return f"temporal_{replay_digest}"

    def temporal_index_revision(self, replay_id: str, source_revision: str | None = None) -> str | None:
        """Return the ready source revision for a replay or a specific index generation."""
        collection = self.get_or_create_collection(self._temporal_collection_name(replay_id, source_revision))
        if not collection:
            return None
        try:
            metadata = collection.metadata or {}
            revision = metadata.get("source_revision")
            if metadata.get("build_state") != "ready":
                return None
            if source_revision is not None and str(revision or "") != str(source_revision):
                return None
            return str(revision) if revision else None
        except Exception as e:
            logger.warning("ChromaDB temporal index metadata read failed: %s", e)
            return None

    def delete_temporal_index(self, replay_id: str, source_revision: str) -> bool:
        """Delete one replay generation; absence is success so retries are idempotent."""
        if not self._available:
            return False
        name = self._temporal_collection_name(replay_id, source_revision)
        try:
            self._client.delete_collection(name=name)
            return True
        except Exception as exc:
            try:
                collections = self._client.list_collections()
                names = {
                    str(item.name if hasattr(item, "name") else item)
                    for item in collections
                }
                if name not in names:
                    return True
            except Exception:
                logger.warning("ChromaDB temporal collection listing failed during delete", exc_info=True)
            logger.warning("ChromaDB temporal index generation delete failed: %s", exc)
            return False

    def list_temporal_index_generations(self) -> list[dict[str, Any]]:
        """List ready replay generations, including older collections predating the SQL ledger."""
        if not self._available:
            return []
        try:
            listed = self._client.list_collections()
        except Exception as exc:
            logger.warning("ChromaDB temporal collection listing failed: %s", exc)
            return []

        generations: list[dict[str, Any]] = []
        for item in listed:
            name = str(item.name if hasattr(item, "name") else item)
            if not name.startswith("temporal_"):
                continue
            try:
                collection = self._client.get_collection(name=name)
                metadata = dict(collection.metadata or {})
                revision = str(metadata.get("source_revision") or "")
                if metadata.get("build_state") != "ready" or not revision:
                    continue
                replay_id = str(metadata.get("replay_id") or "")
                if not replay_id:
                    sample = collection.get(limit=1, include=["metadatas"])
                    rows = sample.get("metadatas") or []
                    replay_id = str(rows[0].get("replay_id") or "") if rows and isinstance(rows[0], dict) else ""
                if not replay_id:
                    continue
                raw_fact_count = metadata.get("source_fact_count")
                fact_count = max(0, int(raw_fact_count)) if raw_fact_count is not None else max(0, int(collection.count()))
                generations.append({
                    "collection_name": name,
                    "replay_id": replay_id,
                    "source_revision": revision,
                    "fact_count": fact_count,
                    "created_at": metadata.get("index_created_at"),
                })
            except Exception as exc:
                logger.warning("Unable to inspect temporal collection metadata: %s", exc)
        return generations

    def begin_temporal_index_build(self, replay_id: str, source_revision: str) -> bool:
        """Mark one immutable index generation unavailable until all batches finish."""
        if not self._available:
            return False
        collection = self.get_or_create_collection(self._temporal_collection_name(replay_id, source_revision))
        if not collection:
            return False
        try:
            metadata = dict(collection.metadata or {})
            metadata["replay_id"] = str(replay_id)[:200]
            metadata["index_created_at"] = datetime.now(timezone.utc).isoformat()
            metadata["build_state"] = "building"
            metadata["target_revision"] = str(source_revision)[:200]
            collection.modify(metadata=metadata)
            return True
        except Exception as e:
            logger.warning("ChromaDB temporal index build start failed: %s", e)
            return False

    def upsert_temporal_fact_batch(
        self,
        replay_id: str,
        facts: list[dict],
        source_revision: str,
    ) -> bool:
        """Upsert one bounded batch into a not-yet-published index generation."""
        if not self._available:
            return False
        if not facts:
            return True
        collection = self.get_or_create_collection(self._temporal_collection_name(replay_id, source_revision))
        if not collection:
            return False
        try:
            ids = [str(item["id"]) for item in facts]
            documents = [self._temporal_fact_to_text(item) for item in facts]
            metadatas = [self._temporal_fact_metadata(replay_id, item) for item in facts]
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            return True
        except Exception as e:
            logger.warning("ChromaDB temporal index batch upsert failed: %s", e)
            return False

    def finalize_temporal_index_build(self, replay_id: str, source_revision: str, fact_count: int) -> bool:
        """Publish a completed generation only after its bounded writes succeed."""
        if not self._available:
            return False
        collection = self.get_or_create_collection(self._temporal_collection_name(replay_id, source_revision))
        if not collection:
            return False
        try:
            metadata = dict(collection.metadata or {})
            if metadata.get("build_state") != "building" or str(metadata.get("target_revision") or "") != str(source_revision):
                return False
            if int(collection.count()) < int(fact_count):
                return False
            metadata["source_revision"] = str(source_revision)[:200]
            metadata["source_fact_count"] = max(0, int(fact_count))
            metadata["build_state"] = "ready"
            metadata.pop("target_revision", None)
            collection.modify(metadata=metadata)
            return True
        except Exception as e:
            logger.warning("ChromaDB temporal index finalization failed: %s", e)
            return False

    def upsert_temporal_facts(
        self,
        replay_id: str,
        facts: list[dict],
        source_revision: str,
    ) -> bool:
        """Build an isolated, replay-scoped candidate index from temporal facts.

        PostgreSQL remains authoritative. The vector index contains only the
        replay's fact text and validity metadata; callers must still verify hits
        against the requested time point before returning them.
        """
        if not self._available or len(facts) > _MAX_TEMPORAL_INDEX_FACTS:
            return False
        collection = self.get_or_create_collection(self._temporal_collection_name(replay_id, source_revision))
        if not collection:
            return False

        try:
            if not self.begin_temporal_index_build(replay_id, source_revision):
                return False
            indexed_ids: list[str] = []
            for start in range(0, len(facts), _TEMPORAL_INDEX_BATCH_SIZE):
                batch = facts[start : start + _TEMPORAL_INDEX_BATCH_SIZE]
                ids = [str(item["id"]) for item in batch]
                if not self.upsert_temporal_fact_batch(replay_id, batch, source_revision):
                    return False
                indexed_ids.extend(ids)

            # Temporal facts are append-only in the replay model, but this
            # cleanup also handles explicit source corrections safely.
            existing = collection.get(where={"replay_id": str(replay_id)}, include=["metadatas"])
            retained = set(indexed_ids)
            stale_ids = [str(item) for item in existing.get("ids", []) if str(item) not in retained]
            for start in range(0, len(stale_ids), _TEMPORAL_INDEX_BATCH_SIZE):
                collection.delete(ids=stale_ids[start : start + _TEMPORAL_INDEX_BATCH_SIZE])

            return self.finalize_temporal_index_build(replay_id, source_revision, len(facts))
        except Exception as e:
            logger.warning("ChromaDB temporal index upsert failed: %s", e)
            return False

    def temporal_semantic_search(
        self,
        replay_id: str,
        query: str,
        at: float,
        n_results: int = 10,
        source_revision: str | None = None,
    ) -> list[dict]:
        """Return temporal fact IDs whose validity interval contains ``at``."""
        query = str(query or "").strip()[:_MAX_SEARCH_QUERY_LENGTH]
        n_results = max(1, min(int(n_results), _MAX_SEARCH_RESULTS))
        if not query or not self._available:
            return []
        collection = self.get_or_create_collection(self._temporal_collection_name(replay_id, source_revision))
        if not collection:
            return []
        try:
            if collection.count() == 0:
                return []
            results = collection.query(
                query_texts=[query],
                n_results=n_results,
                where={
                    "$and": [
                        {"replay_id": str(replay_id)},
                        {"valid_from_ordinal": {"$lte": float(at)}},
                        {"$or": [
                            {"is_open": True},
                            {"valid_to_ordinal": {"$gt": float(at)}},
                        ]},
                    ]
                },
                include=["distances"],
            )
            ids = results.get("ids", [[]])[0]
            distances = results.get("distances", [[]])[0]
            return [
                {
                    "fact_id": str(fact_id),
                    "score": 1.0 - float(distances[index]) if index < len(distances) else 0.0,
                }
                for index, fact_id in enumerate(ids)
            ]
        except Exception as e:
            logger.warning("ChromaDB temporal search failed: %s", e)
            raise RuntimeError("Temporal semantic search failed") from e

    @staticmethod
    def _temporal_fact_metadata(replay_id: str, fact: dict) -> dict[str, Any]:
        valid_to = fact.get("valid_to_ordinal")
        metadata: dict[str, Any] = {
            "replay_id": str(replay_id),
            "subject_id": str(fact.get("subject_id") or ""),
            "predicate": str(fact.get("predicate") or ""),
            "state_key": str(fact.get("state_key") or ""),
            "valid_from_ordinal": float(fact.get("valid_from_ordinal") or 0),
            "is_open": valid_to is None,
        }
        if valid_to is not None:
            metadata["valid_to_ordinal"] = float(valid_to)
        return metadata

    @staticmethod
    def _temporal_fact_to_text(fact: dict) -> str:
        value = json.dumps(fact.get("object_value"), ensure_ascii=False, sort_keys=True, default=str)
        parts = [
            str(fact.get("subject_id") or ""),
            str(fact.get("predicate") or ""),
            str(fact.get("state_key") or ""),
            str(fact.get("object_id") or ""),
            value if value != "null" else "",
        ]
        return " ".join(part for part in parts if part)[:3000]

    def keyword_search(
        self,
        ontology_id: str,
        keyword: str,
        n_results: int = 20,
    ) -> list[dict]:
        """关键词检索 — 过滤 document 中包含 keyword 的结果"""
        keyword = str(keyword or "").strip()[:_MAX_SEARCH_QUERY_LENGTH]
        n_results = max(1, min(int(n_results), _MAX_SEARCH_RESULTS))
        if not keyword:
            return []
        if not self._available:
            return []
        collection = self.get_or_create_collection(f"ontology_{ontology_id}")
        if not collection:
            return []

        try:
            results = collection.query(
                query_texts=[keyword],
                n_results=n_results,
                where_document={"$contains": keyword},
                include=["documents", "metadatas"],
            )
            ids = results.get("ids", [[]])[0]
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            return [
                {"id": ids[i], "document": docs[i], "metadata": metas[i]}
                for i in range(len(ids))
            ]
        except Exception as e:
            logger.warning(f"ChromaDB keyword search error: {e}")
            return []

    def count(self, ontology_id: str) -> int:
        """集合内文档数"""
        if not self._available:
            return 0
        collection = self.get_or_create_collection(f"ontology_{ontology_id}")
        if not collection:
            return 0
        try:
            return collection.count()
        except Exception:
            return 0

    @staticmethod
    def _entity_to_text(entity: dict) -> str:
        """实体 → 嵌入用文本转换"""
        parts = [
            entity.get("name_cn", ""),
            entity.get("name_en", ""),
            entity.get("type", ""),
            entity.get("description", ""),
            json.dumps(entity.get("properties", {}), ensure_ascii=False),
        ]
        return " ".join(p for p in parts if p)


def get_chroma_service() -> ChromaService:
    return ChromaService()
