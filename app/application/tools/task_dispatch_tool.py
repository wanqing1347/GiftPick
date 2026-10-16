# -*- coding: utf-8 -*-
"""task_dispatch 工具

SubAgent as Tool 的调度工具——MainAgent 调它意味着"派一个专家子 Agent 去执行这段 demands"。
2.0 库级没有 subagent 原语，用 FunctionTool 包装子 Agent 实现同等语义。

子 Agent 每次调度新建实例（独立 AgentState = 上下文隔离），只把最终结论回传给 MainAgent，
中间的工具调用过程由业务工具自身通过 EventBus 直接上报前端。

真并行：本工具注册为 is_concurrency_safe，主 Agent 同一轮发起的多个 task_dispatch
会被 2.0 批量 asyncio.gather 并发执行；agent.dispatch 事件带 started_at，
完成时另发 tool.result 带 finished_at/elapsed_ms，可从事件流直接判定时间重叠。

买家偏好由**本工具服务端注入**，不依赖主 Agent 把偏好抄进 demands：
    buyer_id 从 ShoppingContext（ContextVar）取，作用域覆盖到 worker.reply()，
    因此子 Agent 无需改签名就能拿到偏好。以前靠提示词要求“demands 必须自包含偏好”
    是**软约束**：模型漏抄就静默降级为无偏好推荐，不报错、无告警。

注意：本模块不能用 `from __future__ import annotations`（AgentScope schema 生成依赖运行时注解）。
"""
import logging
import json
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal, Optional

from agentscope.message import TextBlock, ToolResultState, UserMsg
from agentscope.tool import ToolChunk

from app.application.agents.product_agent import ProductAgentFactory
from app.application.agents.research_agent import ResearchAgentFactory
from app.application.memory.preference_selector import (
    PreferenceSelector,
    render_preference_hint,
)
from app.domain.buyer.preference import PreferenceStore
from app.domain.buyer.recipient_memory import normalize_recipient_label
from app.application.memory.gift_intent import recipient_label_is_mentioned, recipient_labels_in_text
from app.application.tools.recipient_memory_tools import (
    recipient_material_exclusion_tags, render_recipient_memory_hint,
)
from app.infrastructure.context import ShoppingContext, RecipientSearchScope
from app.infrastructure.eventbus import TradeEventBus, observe_run_events
from app.infrastructure.budget import get_budget, rule_fallback_text

logger = logging.getLogger(__name__)


def build_task_dispatch_tool(
    product_factory: ProductAgentFactory,
    research_factory: ResearchAgentFactory,
    bus: TradeEventBus,
    preference_store: Optional[PreferenceStore] = None,
    preference_selector: Optional[PreferenceSelector] = None,
    preference_top_k: int = 5,
    subagent_inject: bool = True,
    recipient_memory_store=None,
):
    selector = preference_selector or PreferenceSelector()

    async def _preference_hint(subagent_type: str, demands: str):
        """给子 Agent 算出要注入的偏好块；无需注入时返回 None。

        product_agent 与 research_agent 都注入：
        product_agent 按买家口味筛选商品候选；research_agent 按同一批偏好取舍
        调研结论的口径（买家明确避开的风格不能被外部口碑悄悄推翻）。
        """
        if not (subagent_inject and preference_store
                and subagent_type in ("product_agent", "research_agent")):
            return None
        snapshot = ShoppingContext.current()
        if snapshot is not None and snapshot.gift_mode:
            return None
        buyer_id = snapshot.buyer_id if snapshot else ""
        if not buyer_id:
            return None
        try:
            preferences = await preference_store.list_by_buyer(buyer_id)
            selected = await selector.select(preferences, query=demands, top_k=preference_top_k)
        except Exception as err:  # noqa: BLE001 —— 读记忆失败不能让派发挂掉
            logger.warning("子 Agent 偏好注入跳过（读取失败）：%s", err)
            return None
        return render_preference_hint(selected) if selected else None

    def _research_rejection(snapshot, demands: str, recipient_label: str):
        """同步复查父级共享范围，入口和异步准备后的派发边界使用同一规则。"""
        if snapshot is not None and snapshot.recipient_search_scope.recipient_lookup_attempted:
            return ToolChunk(content=[TextBlock(type="text", text="[error] 本轮已尝试读取收礼人记忆，不能派发自由文本外部调研，请使用目录检索")],
                             state=ToolResultState.ERROR)
        if snapshot is not None and snapshot.recipient_search_scope.has_private_facts:
            return ToolChunk(content=[TextBlock(type="text", text="[error] 本轮已读取收礼人私有事实，无法安全核验自由文本外部调研请求，请仅使用目录检索")],
                             state=ToolResultState.ERROR)
        if recipient_label or (snapshot is not None and snapshot.gift_mode):
            explicit_labels = (*snapshot.recipient_labels, *recipient_labels_in_text(snapshot.raw_query)) if snapshot is not None else ()
            if recipient_label or explicit_labels:
                return ToolChunk(content=[TextBlock(type="text", text="[error] 本轮明确指定了收礼人，不能派发自由文本外部调研，请使用目录检索")],
                                 state=ToolResultState.ERROR)
            # 仅词面纵深保护：识别已召回标签/完整陈述的大小写与空白变体，不宣称能识别语义改写。
            normalized_demands = "".join(demands.casefold().split())
            private_terms = (*snapshot.recipient_labels, *recipient_labels_in_text(snapshot.raw_query),
                             *snapshot.recipient_search_scope.private_terms) if snapshot is not None else ()
            if recipient_label or any("".join(term.casefold().split()) in normalized_demands for term in private_terms if term):
                return ToolChunk(content=[TextBlock(type="text", text="[error] 联网调研只能接收公开商品/型号或公共问题，请移除收礼人标签和私有事实")],
                                 state=ToolResultState.ERROR)
        return None

    async def task_dispatch(
        subagent_type: Literal["product_agent", "research_agent"],
        demands: str,
        recipient_label: str = "",
    ) -> ToolChunk:
        """调度专家子代理执行子任务，返回子代理的结论（JSON 字符串）。

        仅当子任务满足"可并行 / 需要上下文隔离 / 内部调用链较深"任一条件时使用；
        简单的单步工具调用应自己直接调业务工具完成。
        多个彼此独立的子任务请在同一轮一次性发起多个本工具调用，系统会并发执行。

        Args:
            subagent_type (`str`):
                子代理类型："product_agent"（商品检索专家）或 "research_agent"（联网调研专家）。
            demands (`str`):
                本子任务所需的指令和约束，如预算、目的地、product_id/sku_id；子代理看不到主对话历史，
                应提供完成本子任务依赖的任务上下文。买家偏好由服务端注入，无需在 demands 中重复。
                送礼时联网调研只传商品/型号或公共问题，禁止传收礼人标签和私有事实。
            recipient_label (`str`): 本轮买家明确给出的精确收礼人标签；仅检索代理使用。
                标签只去首尾空白，不推断别名；收礼人事实由服务端注入，无需抄入 demands。
        """
        session_id = ShoppingContext.current_session_id()
        snapshot = ShoppingContext.current()
        if subagent_type == "research_agent":
            rejection = _research_rejection(snapshot, demands, recipient_label)
            if rejection is not None:
                return rejection
            if not getattr(research_factory, "search_enabled", True):
                return ToolChunk(
                    content=[TextBlock(type="text", text="[error] 外部联网调研不可用：未配置 Tavily 搜索能力（TAVILY_API_KEY）")],
                    state=ToolResultState.ERROR,
                )
        budget = get_budget()
        if budget is not None and budget.exhausted:
            return ToolChunk(content=[TextBlock(text=rule_fallback_text())], state=ToolResultState.SUCCESS)
        if subagent_type == "product_agent":
            worker = product_factory.build()
        elif subagent_type == "research_agent":
            worker = research_factory.build()
        else:
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] 未知 subagent_type：{subagent_type}")],
                state=ToolResultState.ERROR,
            )

        inputs = [UserMsg("commerce_concierge", demands)]
        worker_snapshot = snapshot
        if recipient_label or (snapshot is not None and snapshot.gift_mode):
            # 每次派发独立 ContextVar 票据；并发收礼人的硬约束不能串台。
            if snapshot is not None:
                worker_snapshot = replace(snapshot, gift_mode=True, excluded_material_tags=(),
                                          recipient_search_scope=RecipientSearchScope(),
                                          recipient_labels=() if subagent_type == "research_agent" else snapshot.recipient_labels,
                                          bound_recipient_label="",
                                          raw_query="" if subagent_type == "research_agent" else snapshot.raw_query)
            if subagent_type == "product_agent" and recipient_label:
                if snapshot is None or recipient_memory_store is None:
                    return ToolChunk(content=[TextBlock(type="text", text="[error] 收礼人检索缺少可信上下文或记忆存储")],
                                     state=ToolResultState.ERROR)
                try:
                    label = normalize_recipient_label(recipient_label)
                    if not recipient_label_is_mentioned(label, snapshot.raw_query):
                        raise ValueError("收礼人标签未在本轮买家原文中原样出现")
                    ShoppingContext.mark_recipient_lookup_attempted()
                    records = await recipient_memory_store.list_for_recipient(snapshot.buyer_id, label)
                    records = [r for r in records if r.buyer_id == snapshot.buyer_id and r.recipient_label == label]
                    # 共享父级隐私标记必须在 worker 执行前更新；不覆盖父级按标签的材质范围。
                    ShoppingContext.mark_recipient_facts(label, tuple(r.statement for r in records))
                    tags = tuple(recipient_material_exclusion_tags(records))
                    worker_snapshot = replace(worker_snapshot, excluded_material_tags=tags, bound_recipient_label=label,
                                              recipient_search_scope=RecipientSearchScope(
                                                  material_tags_by_label={label: tags},
                                                  private_terms=(label, *(r.statement for r in records)),
                                                  has_private_facts=bool(records)))
                    if records:
                        inputs.insert(0, UserMsg("recipient_memory_hint", render_recipient_memory_hint(records)))
                except Exception as err:
                    logger.warning("收礼人检索记忆读取失败：%s", type(err).__name__)
                    return ToolChunk(content=[TextBlock(type="text", text="[error] 收礼人记忆读取失败，请稍后重试")],
                                     state=ToolResultState.ERROR)
        else:
            hint = await _preference_hint(subagent_type, demands)
            if hint:
                inputs.insert(0, UserMsg("memory_hint", hint))

        evidence_refs, verified_ids = set(), set()
        def capture(event):
            if event.shopping_session_id != session_id or event.type != "tool.result" or not isinstance(event.payload, dict):
                return
            payload = event.payload
            if payload.get("tool") != "product_search_tool":
                return
            if payload.get("result_ref"):
                evidence_refs.add(payload["result_ref"])
            hits = payload.get("hits", [])
            if not isinstance(hits, list):
                return
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                product_id = hit.get("product_id")
                if isinstance(product_id, str) and product_id:
                    verified_ids.add(product_id)
                skus = hit.get("skus", [])
                if isinstance(skus, list):
                    verified_ids.update(
                        sku["sku_id"] for sku in skus
                        if isinstance(sku, dict) and isinstance(sku.get("sku_id"), str) and sku["sku_id"]
                    )
        # 异步准备期间其他工具可标记读取尝试；最终复查到 worker.reply 之间不能再 await。
        if subagent_type == "research_agent":
            rejection = _research_rejection(snapshot, demands, recipient_label)
            if rejection is not None:
                return rejection
        context_token = ShoppingContext.set(worker_snapshot)
        try:
            with observe_run_events(capture):
                # 仅实际执行 worker 时发出开始事件；计时不包含记忆/偏好准备。
                started_at = datetime.now(timezone.utc).isoformat()
                started_monotonic = time.monotonic()
                bus.publish(
                    session_id,
                    "agent.dispatch",
                    {"agent": subagent_type, "demands": demands, "started_at": started_at},
                )
                reply = await worker.reply(inputs)
        finally:
            ShoppingContext.reset(context_token)
        output = reply.get_text_content() or ""
        mentioned_ids = set(re.findall(r"\bP\d{4}(?:-S\d+)?\b", output))
        unknown = mentioned_ids - verified_ids
        if unknown:
            identifier_status = "unverified_identifiers_present"
        elif mentioned_ids:
            identifier_status = "mentioned_identifiers_found_in_tool_evidence"
        else:
            identifier_status = "no_identifiers_mentioned"
        decision = {
            "agent": subagent_type,
            "execution_status": "completed",
            "summary": output,
            "evidence_refs": sorted(evidence_refs),
            "identifier_evidence": {
                "status": identifier_status,
                "scope": "product_search_tool hits 中 product_id / sku_id 的字面出现",
                "verified_ids": sorted(mentioned_ids & verified_ids),
                "unverified_ids": sorted(unknown),
                "note": "仅核对商品/SKU 标识是否出现在本轮实际商品检索工具证据中；不核验摘要中的价格、库存、属性或其他陈述。",
            },
        }
        bus.publish(
            session_id,
            "tool.result",
            {
                "tool": "task_dispatch",
                "agent": subagent_type,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": round((time.monotonic() - started_monotonic) * 1000),
            },
        )
        return ToolChunk(
            content=[TextBlock(type="text", text=json.dumps(decision, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    return task_dispatch
