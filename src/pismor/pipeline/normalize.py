"""事件规范化：把 conversation / text 两种入口收成下游唯一消费的管线视图。

`event_type` 在管线内的读取收敛到本模块；下游按 `mode` 判断策略、按
`user_texts` 取用户域证据、按 `prompt_text` 送抽取。外部契约不变：
intake 已完成 pydantic 校验并把 `event_type` / `force_extract` 回写原始 dict。
"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """管线视图：只承载策略相关派生字段，不复制 source/user_ref/ts 等事件本体。"""

    event_type: Literal["conversation", "text"]  # 唯一用途：sources.origin_type 落库
    mode: Literal["dialogue", "document"]
    prompt_text: str          # 送抽取 LLM 的完整文本（dialogue 含 assistant 上下文）
    user_texts: tuple[str, ...]  # 用户域文本（dialogue 为全部非空 user 消息）
    force_extract: bool       # 上报方显式要求抽取

    @property
    def is_dialogue(self) -> bool:
        return self.mode == "dialogue"

    @property
    def last_user_text(self) -> str:
        return self.user_texts[-1] if self.user_texts else ""


def render_messages(messages: list[dict]) -> str:
    """对话渲染为抽取 prompt 的文本：role 前缀保留，assistant 仅作上下文。"""
    return "\n".join(f"{m.get('role')}: {m.get('text', '')}" for m in messages)


def normalize(event: dict) -> NormalizedEvent:
    """原始事件 dict → 管线视图。intake 未回写 event_type 时按 text 兜底。"""
    event_type = event.get("event_type") or "text"
    if event_type == "conversation":
        messages = event.get("messages") or []
        return NormalizedEvent(
            event_type="conversation",
            mode="dialogue",
            prompt_text=render_messages(messages),
            user_texts=tuple(
                m.get("text", "") for m in messages
                if m.get("role") == "user" and (m.get("text") or "").strip()
            ),
            force_extract=event.get("force_extract") is True,
        )
    content = event.get("content") or ""
    return NormalizedEvent(
        event_type="text",
        mode="document",
        prompt_text=content,
        user_texts=(content,) if content else (),
        force_extract=False,
    )
