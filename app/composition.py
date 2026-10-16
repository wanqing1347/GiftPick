# -*- coding: utf-8 -*-
"""装配容器（Composition Root）

API 进程与 worker 进程共用同一份接线，避免两处各自 new 一套导致行为漂移。
洋葱由内向外装配：infrastructure → application → （presentation 在 server.py）。

所有外部依赖都是可选的，按「不配就降级」设计：
    DATABASE_URL 未配 → SQLite；= "file" → JSON 文件存储
    REDIS_URL    未配 → 无缓存、无队列、无跨进程事件背板
    QUEUE_ENABLED=0  → 不入队，请求在 API 进程内直接跑（三期行为）
"""
from __future__ import annotations

import hashlib
import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.application.agents.main_agent import MainAgentFactory, SessionRegistry
from app.application.agents.orchestrator import MainAgentOrchestrator
from app.application.agents.product_agent import ProductAgentFactory
from app.application.agents.research_agent import ResearchAgentFactory
from app.application.harness.assertions import SequencingTracker
from app.application.harness.loop_detector import LoopDetector
from app.application.memory.preference_selector import PreferenceSelector
from app.infrastructure.buyer_skills import BuyerSkillStore
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.queue.ports.task_queue import TaskQueue
from app.infrastructure.cache.cached_embedding_client import CachedEmbeddingClient
from app.infrastructure.cache.redis_cache import RedisCache
from app.infrastructure.cache.semantic_cache import SemanticCache
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.persistence.in_memory_repositories import (
    InMemoryProductRepository,
)
from app.infrastructure.persistence.json_file_stores import (
    JsonFileConversationStore,
    JsonFilePreferenceStore,
    JsonFileSessionStore,
)
from app.infrastructure.persistence.sql.repositories import (
    SqlConversationStore,
    SqlPreferenceStore,
    SqlSessionStore,
    bootstrap_schema,
    create_engine,
)
from app.infrastructure.queue.redis_stream_queue import (
    RedisEventBackplane,
    RedisStreamTaskQueue,
)
from app.infrastructure.rag.category_knowledge import (
    bootstrap_category_knowledge,
    build_category_knowledge_base,
)
from app.infrastructure.rerank.http_reranker import HttpReranker
from app.infrastructure.resilience import CircuitBreakerRegistry
from app.infrastructure.settings import Settings, load_settings
from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.prompt_registry import PromptRegistry, toolset_contract
from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.shared_throttle import RedisGatewayThrottle
from app.infrastructure.tracing import setup_tracing, shutdown_tracing
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.queue.archive import QueueArchive
from app.infrastructure.capability_registry import CapabilityRegistry
from app.presentation.ag_ui_runtime import AGUIRuntime
from app.infrastructure.runtime_version import app_source_fingerprint
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.embedding_identity import embedding_identity
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex

logger = logging.getLogger(__name__)


def _prompt_fingerprint() -> str:
    """提示词文件指纹，用作语义缓存 namespace 的一部分。

    prompt 一改，旧缓存的回复就不再代表当前 Agent 行为，必须作废。
    读不到文件时返回固定值，不因此阻断启动。
    """
    path = Path(__file__).resolve().parent / "application" / "prompts" / "giftpick.yml"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    except OSError:
        return "noprompt"


@dataclass
class Container:
    settings: Settings
    bus: TradeEventBus
    orchestrator: MainAgentOrchestrator
    cache: RedisCache
    semantic_cache: SemanticCache
    task_queue: Optional[TaskQueue]
    backplane: Optional[RedisEventBackplane]
    product_repo: InMemoryProductRepository
    embedder: Any
    vector_index: QdrantProductIndex
    knowledge_base: Any
    db_engine: Any
    runtime: dict = field(default_factory=dict)
    ag_ui_runtime: Any = None
    session_store: Any = None
    identity_policy: Any = None
    prompt_registry: Any = None
    context_service: Any = None

    async def startup(self) -> None:
        """建表 / 建向量库 / 建知识库。任一失败只告警，对应能力降级但服务可用。"""
        if self.context_service is not None:
            await self.context_service.startup()
        if self.ag_ui_runtime is not None:
            await self.ag_ui_runtime.startup()
        if self.db_engine is not None:
            try:
                await bootstrap_schema(self.db_engine)
            except Exception as err:  # noqa: BLE001
                logger.warning("数据库建表失败，持久化能力不可用：%s", err)
        if isinstance(self.task_queue, RedisStreamTaskQueue):
            try:
                await self.task_queue.ensure_group()
            except Exception as err:  # noqa: BLE001
                logger.warning("队列消费者组创建失败：%s", err)
        await bootstrap_product_index(self.product_repo, self.embedder, self.vector_index)
        await bootstrap_category_knowledge(self.knowledge_base)

    async def shutdown(self) -> None:
        if self.context_service is not None:
            await self.context_service.shutdown()
        if self.ag_ui_runtime is not None:
            await self.ag_ui_runtime.shutdown()
        if isinstance(self.session_store, JsonFileSessionStore):
            await self.session_store.close()
        await self.vector_index.close()
        await self.cache.close()
        if self.db_engine is not None:
            await self.db_engine.dispose()
        await asyncio.to_thread(shutdown_tracing)


async def build_container() -> Container:
    source_fingerprint = app_source_fingerprint()
    settings = load_settings()
    identity_policy = IdentityPolicy.from_settings(settings)
    project_root = Path(__file__).resolve().parent.parent
    prompt_registry = PromptRegistry(settings.data_dir / "prompts" / "registry.sqlite3",
        toolset_contract(project_root, web_search_enabled=bool(settings.tavily_api_key)), pinned_version=settings.prompt_pin_version)
    await asyncio.to_thread(prompt_registry.bootstrap, project_root / "app/application/prompts/giftpick.yml")
    setup_tracing(settings)

    # ---- Infrastructure ----
    product_repo = InMemoryProductRepository()
    bus = TradeEventBus()
    vector_index = QdrantProductIndex(settings)

    cache = RedisCache(settings.redis_url)
    raw_embedder = OpenAIEmbeddingClient(settings)
    embedder = (
        CachedEmbeddingClient(raw_embedder, cache, embedding_identity(settings))
        if cache.enabled
        else raw_embedder
    )
    semantic_cache = SemanticCache(
        cache,
        embedder,
        threshold=settings.semantic_cache_threshold,
        enabled=settings.semantic_cache_enabled,
        # 模型名 + 提示词指纹入 key：改 prompt 或换模型后旧回复自动失效
        namespace=f"{settings.llm_model}:{_prompt_fingerprint()}",
    )
    knowledge_base = build_category_knowledge_base(settings)

    # 队列与事件背板都依赖 Redis：没有 Redis 就退回单进程直跑
    task_queue: Optional[TaskQueue] = None
    backplane: Optional[RedisEventBackplane] = None
    if cache.enabled and settings.queue_enabled:
        task_queue = RedisStreamTaskQueue(cache.client, QueueArchive(settings.data_dir / "queue_archive.db"))
        backplane = RedisEventBackplane(cache.client)
        # 关键：worker 与 API 是两个进程，不接背板前端收不到 worker 产生的事件
        bus.attach_backplane(backplane)
        logger.info("队列已启用（Redis Stream），事件走跨进程背板")
    else:
        logger.info("队列未启用，意图在当前进程内直接执行")

    # 存储形态
    use_database = settings.database_url != "file"
    db_engine = create_engine(settings.database_url) if use_database else None
    if db_engine is not None:
        preference_store = SqlPreferenceStore(db_engine)
        session_store = SqlSessionStore(db_engine)
        conversation_store = SqlConversationStore(db_engine)
        logger.info("持久化形态：%s", db_engine.url.get_backend_name())
    else:
        preference_store = JsonFilePreferenceStore(settings.data_dir)
        session_store = JsonFileSessionStore(settings.data_dir)
        conversation_store = JsonFileConversationStore(settings.data_dir)
        logger.info("持久化形态：本地 JSON 文件（DATABASE_URL=file）")

    # 熔断注册表：开 BREAKER_SHARED 且 Redis 可用时跨实例共享，否则进程内
    if settings.breaker_shared and cache.enabled:
        circuit_registry = SharedCircuitBreakerRegistry(
            cache,
            failure_threshold=settings.tool_failure_threshold,
            reset_seconds=settings.tool_circuit_reset_seconds,
        )
        logger.info("熔断状态：Redis 跨实例共享")
    else:
        circuit_registry = CircuitBreakerRegistry(
            failure_threshold=settings.tool_failure_threshold,
            reset_seconds=settings.tool_circuit_reset_seconds,
        )
    # 全进程唯一的网关配额闸门：三个 Agent 工厂共用，否则各限一份等于没限
    throttle = (
        RedisGatewayThrottle(
            cache.client,
            max_concurrency=settings.llm_max_concurrency,
            min_interval_seconds=settings.llm_min_interval_seconds,
            namespace=f"{settings.llm_base_url}\n{settings.llm_model}",
        ) if cache.enabled else GatewayThrottle(
            max_concurrency=settings.llm_max_concurrency,
            min_interval_seconds=settings.llm_min_interval_seconds,
        )
    )
    # 护栏判定器同样全进程唯一：按会话累积状态，需跨 Agent 实例与轮次共享
    sequencing_tracker = SequencingTracker()
    loop_detector = LoopDetector(repeat_threshold=settings.loop_repeat_threshold)
    from app.infrastructure.rerank.factory import create_reranker
    reranker = create_reranker(settings, throttle=throttle, bus=bus)

    # ---- Application ----
    catalog_search = CatalogSearchUseCase(
        product_repo, embedder=embedder, vector_index=vector_index, reranker=reranker,
        hybrid_enabled=settings.hybrid_recall_enabled,
        hybrid_lexical_weight=settings.hybrid_lexical_weight,
        hybrid_vector_weight=settings.hybrid_vector_weight,
        recall_candidates=settings.recall_candidates,
    )
    product_factory = ProductAgentFactory(
        settings, catalog_search, bus, knowledge_base, circuit_registry, throttle,
    )
    research_factory = ResearchAgentFactory(settings, bus, circuit_registry, throttle)
    # 规范化事实与向量同库提交；默认长期记忆走语义检索。
    from app.infrastructure.semantic_memory import SemanticPreferenceStore, PreferenceDistiller
    from app.infrastructure.llm import create_chat_model
    preference_store = SemanticPreferenceStore(
        settings.data_dir / "buyer_memory.db", preference_store,
        PreferenceDistiller(create_chat_model(settings, stream=False, throttle=throttle, bus=bus)),
        embedder, settings.embedding_model + ":" + str(settings.embedding_dim),
    )
    preference_selector = preference_store
    from app.infrastructure.shopping_forms import ShoppingFormStore
    from app.infrastructure.recipient_memory import RecipientMemoryStore
    main_factory = MainAgentFactory(
        settings, product_factory, research_factory, bus, preference_store, circuit_registry, throttle,
        sequencing=sequencing_tracker,
        loop_detector=loop_detector,
        preference_selector=preference_selector,
        capability_registry=CapabilityRegistry(settings.data_dir / "capabilities.db"),
        buyer_skill_store=BuyerSkillStore(settings.data_dir / "buyer_skills.db"),
        shopping_form_store=ShoppingFormStore(settings.data_dir / "shopping_forms.db"),
        recipient_memory_store=RecipientMemoryStore(settings.data_dir / "recipient_memory.db"),
    )
    sessions = SessionRegistry(main_factory, session_store, enforce_owner=settings.session_owner_binding, prompt_registry=prompt_registry)
    orchestrator = MainAgentOrchestrator(
        sessions, bus, preference_store, conversation_store, semantic_cache,
        output_guard_enabled=settings.output_guard_enabled,
        loop_detector=loop_detector,
        token_budget_total=settings.token_budget_total,
        preference_selector=preference_selector,
        preference_top_k=settings.preference_top_k,
        session_lease_factory=task_queue.session_lease if task_queue is not None else None,
        evidence_store=product_factory.evidence_store,
    )

    from app.application.agents.context_service import ContextService
    return Container(
        settings=settings,
        bus=bus,
        orchestrator=orchestrator,
        context_service=ContextService(orchestrator, session_store, product_factory.evidence_store, None, settings),
        cache=cache,
        semantic_cache=semantic_cache,
        task_queue=task_queue,
        backplane=backplane,
        product_repo=product_repo,
        embedder=embedder,
        vector_index=vector_index,
        knowledge_base=knowledge_base,
        db_engine=db_engine,
        runtime={"app_source_sha256": source_fingerprint},
        ag_ui_runtime=AGUIRuntime(AGUIJournal(settings.data_dir / "ag_ui_runs.db"), orchestrator),
        session_store=session_store,
        identity_policy=identity_policy,
        prompt_registry=prompt_registry,
    )
