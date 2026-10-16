"""买家私有的收礼人事实；标签只去除首尾空白，保持精确匹配。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

VALID_CHANNELS = ("like", "dislike", "interest", "constraint", "gift_feedback")


def normalize_recipient_label(recipient_label: str) -> str:
    if not isinstance(recipient_label, str) or not recipient_label.strip():
        raise ValueError("收礼人标签不能为空")
    return recipient_label.strip()


@dataclass(frozen=True)
class RecipientMemory:
    buyer_id: str
    recipient_label: str
    channel: str
    statement: str
    memory_id: str = ""
    version: int = 1
    created_at: str = ""
    source_kind: str = "user"
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.buyer_id, str) or not self.buyer_id.strip():
            raise ValueError("买家身份不能为空")
        object.__setattr__(self, "recipient_label", normalize_recipient_label(self.recipient_label))
        if self.channel not in VALID_CHANNELS:
            raise ValueError(f"RecipientMemory.channel 必须是 {VALID_CHANNELS}：{self.channel}")
        if not isinstance(self.statement, str) or not self.statement.strip():
            raise ValueError("收礼人记忆陈述不能为空")
        statement = self.statement.strip()
        if len(statement) > 500:
            raise ValueError("收礼人记忆不能超过 500 字符")
        object.__setattr__(self, "statement", statement)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("记忆版本必须是正整数")
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc).isoformat())
