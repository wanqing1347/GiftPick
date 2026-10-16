# -*- coding: utf-8 -*-
"""ResearchAgent

联网调研专家。基于 AgentScope 2.0 Agent，工具集：
    web_search_tool    （Tavily，可选，有 TAVILY_API_KEY 才注册）
    fetch_webpage_tool （抓网页正文，常驻）

通过 task_dispatch 工具被 MainAgent 调度（每次调度新建独立实例 → 上下文隔离）。
调研需要"搜索→读正文→改写再搜"的多轮深链，抓回的长正文不宜污染主对话，
这是派发它的标准理由。目录内商品事实以 ProductAgent/商品卡为准，本代理只补充
外部口碑与时效信息。
"""
from __future__ import annotations
from app.infrastructure.context_governance import ContextAwareAgent

from agentscope.agent import Agent, ReActConfig
from agentscope.tool import FunctionTool, Toolkit

from app.application.agents.context_policy import build_context_config
from app.application.prompts.loader import load_prompts
from app.application.tools.fetch_webpage_tool import build_webpage_fetch_tool
from app.application.tools.web_search_tool import build_web_search_tool
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.llm import create_chat_model
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.resilience import (
    CircuitBreakerRegistry,
    ToolResilienceMiddleware,
)
from app.infrastructure.settings import Settings
from app.infrastructure.tracing import build_agent_middlewares
from app.application.harness.assertions import SequencingTracker
from app.application.harness.loop_detector import LoopDetector
from app.infrastructure.harness_middleware import HarnessToolMiddleware


class ResearchAgentFactory:
    def __init__(
        self,
        settings: Settings,
        bus: TradeEventBus,
        circuit_registry: CircuitBreakerRegistry,
        throttle: GatewayThrottle,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._circuit_registry = circuit_registry
        # 闸门由组装根下发，各工厂必须共用同一个，否则各限一份等于没限
        self._throttle = throttle
        self.bind_harness(SequencingTracker(), LoopDetector(repeat_threshold=settings.loop_repeat_threshold))

    @property
    def search_enabled(self) -> bool:
        """Tavily 搜索能力是否已配置；仅有网页抓取不足以执行外部调研。"""
        return bool(self._settings.tavily_api_key and self._settings.tavily_api_key.strip())

    def bind_harness(self, sequencing, loop_detector) -> None:
        """主 Agent 和子 Agent 共用会话护栏，避免派发后重新计数。"""
        self._sequencing, self._loop_detector = sequencing, loop_detector

    def _resilience(self) -> list:
        chain = [HarnessToolMiddleware(sequencing=self._sequencing,
            loop_detector=self._loop_detector, bus=self._bus)] if self._settings.harness_enabled else []
        return [*chain, ToolResilienceMiddleware(self._circuit_registry, self._bus)]

    def build_tools(self) -> list[FunctionTool]:
        """ResearchAgent 的业务工具集（均带超时+熔断保护）。

        web_search_tool 按"有 TAVILY_API_KEY 才注册"设计；未配置时仍可构造抓取工具，
        但缺少搜索入口不能完成调研，task_dispatch 会在构造代理前拒绝派发。
        """
        tools = [
            FunctionTool(
                build_webpage_fetch_tool(self._bus),
                is_read_only=True,
                middlewares=self._resilience(),
            ),
        ]
        if self.search_enabled:
            tools.append(
                FunctionTool(
                    build_web_search_tool(self._settings, self._bus),
                    is_read_only=True,
                    middlewares=self._resilience(),
                ),
            )
        return tools

    def build(self) -> Agent:
        prompts = load_prompts()["sub_agents"]["research"]
        return ContextAwareAgent(
            name=prompts["name"],
            system_prompt=prompts["system_prompt"],
            model=create_chat_model(self._settings, throttle=self._throttle, bus=self._bus),
            toolkit=Toolkit(tools=list(self.build_tools())),
            middlewares=build_agent_middlewares(self._settings),
            context_config=build_context_config(
                self._settings.context_size,
                self._settings.tool_result_limit,
            ),
            react_config=ReActConfig(max_iters=6),
        )
