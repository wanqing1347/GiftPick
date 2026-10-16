# -*- coding: utf-8 -*-
"""ShoppingContext

用 ContextVar 保存当前任务的会话快照（shopping_session_id / buyer_id / locale / currency），
跨层透明传递：工具与子 Agent 执行时随时读取，无需层层透传参数。
多用户并发任务依赖 asyncio Task 级隔离，不会串台。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Optional


@dataclass
class RecipientSearchScope:
    """仅当轮共享，SDK 工具子 Task 更新后下一次目录工具仍能读取。"""
    material_tags_by_label: dict[str, tuple[str, ...]] = field(default_factory=dict)
    private_terms: tuple[str, ...] = field(default=(), repr=False)
    recipient_lookup_attempted: bool = False
    has_private_facts: bool = False


@dataclass(frozen=True)
class ShoppingContextSnapshot:
    shopping_session_id: str
    buyer_id: str
    locale: str
    currency: str
    # 从长期 dislike 偏好推导出的结构化材质黑名单；工具入口会与模型入参取并集。
    excluded_material_tags: tuple[str, ...] = ()
    session_fence: int = 0
    prompt_version: str = ""
    prompt_variant: str = ""
    prompt_deployment_id: str = ""
    capability_digest: str = ""
    skill_catalog_mode: str = "legacy"
    prompt_document_json: str = field(default="", repr=False)
    gift_mode: bool = False
    raw_query: str = field(default="", repr=False)
    recipient_labels: tuple[str, ...] = field(default=(), repr=False)
    bound_recipient_label: str = field(default="", repr=False)
    recipient_search_scope: RecipientSearchScope = field(default_factory=RecipientSearchScope, repr=False, compare=False)


_current_snapshot: ContextVar[Optional[ShoppingContextSnapshot]] = ContextVar(
    "giftpick_shopping_context",
    default=None,
)


class ShoppingContext:
    @staticmethod
    def set_skill_catalog_mode(mode: str) -> None:
        snapshot = _current_snapshot.get()
        if snapshot is None or mode not in {"legacy", "append_only"}:
            raise ValueError("Skill 注入模式或买家上下文无效")
        _current_snapshot.set(replace(snapshot, skill_catalog_mode=mode))

    @staticmethod
    def set(snapshot: ShoppingContextSnapshot):
        return _current_snapshot.set(snapshot)

    @staticmethod
    def reset(token) -> None:
        _current_snapshot.reset(token)

    @staticmethod
    def current() -> Optional[ShoppingContextSnapshot]:
        return _current_snapshot.get()

    @staticmethod
    def set_gift_scope(active: bool, labels: tuple[str, ...]) -> None:
        snapshot = _current_snapshot.get()
        if snapshot is not None:
            _current_snapshot.set(replace(snapshot, gift_mode=active, recipient_labels=labels))

    @staticmethod
    def recipient_material_tags(label: str) -> tuple[str, ...]:
        snapshot = _current_snapshot.get()
        return snapshot.recipient_search_scope.material_tags_by_label.get(label, ()) if snapshot else ()

    @staticmethod
    def set_recipient_material_tags(label: str, tags: list[str], statements: tuple[str, ...] = ()) -> None:
        snapshot = _current_snapshot.get()
        if snapshot is None or not snapshot.gift_mode:
            return
        # 每个精确标签独立保存材质范围；不落盘、不同轮次使用新容器。
        snapshot.recipient_search_scope.material_tags_by_label[label] = tuple(dict.fromkeys(tags))
        ShoppingContext.mark_recipient_facts(label, statements)

    @staticmethod
    def mark_recipient_facts(label: str, statements: tuple[str, ...]) -> None:
        """任意选购模式读取私有事实后标记本请求，和材质过滤的 gift 范围分开。"""
        snapshot = _current_snapshot.get()
        if snapshot is None:
            return
        snapshot.recipient_search_scope.private_terms = tuple(dict.fromkeys(
            (*snapshot.recipient_search_scope.private_terms, label, *statements)))
        snapshot.recipient_search_scope.has_private_facts |= bool(statements)

    @staticmethod
    def mark_recipient_lookup_attempted() -> None:
        """在存储 await 前同步记录读取尝试，不表示已读到私有事实。"""
        snapshot = _current_snapshot.get()
        if snapshot is not None:
            snapshot.recipient_search_scope.recipient_lookup_attempted = True

    @staticmethod
    def current_session_id() -> str:
        snapshot = _current_snapshot.get()
        return snapshot.shopping_session_id if snapshot else "anonymous"

    @staticmethod
    def set_excluded_material_tags(tags: list[str]) -> None:
        snapshot = _current_snapshot.get()
        if snapshot is None:
            return
        _current_snapshot.set(replace(snapshot, excluded_material_tags=tuple(dict.fromkeys(tags))))

    @staticmethod
    def set_session_fence(fence: int) -> None:
        snapshot = _current_snapshot.get()
        if snapshot is None:
            raise RuntimeError("不能为缺少当前上下文的会话设置 fence")
        _current_snapshot.set(replace(snapshot, session_fence=fence))

    @staticmethod
    def set_prompt_assignment(assignment: dict) -> None:
        import json
        snapshot = _current_snapshot.get()
        if snapshot is None:
            raise RuntimeError("Prompt 分组需要当前会话上下文")
        _current_snapshot.set(replace(snapshot, prompt_version=assignment["version_id"],
            prompt_variant=assignment["variant"], prompt_deployment_id=assignment["deployment_id"],
            prompt_document_json=json.dumps(assignment["document"], ensure_ascii=False)))

    @staticmethod
    def set_capability_digest(digest: str) -> None:
        snapshot = _current_snapshot.get()
        if snapshot is None:
            raise RuntimeError("能力版本绑定需要当前会话上下文")
        _current_snapshot.set(replace(snapshot, capability_digest=digest))
