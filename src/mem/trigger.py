"""触发分级（spec §10 成本控制核心，仅作用于 conversation）：

explicit 声明与反馈事件零阈值直通；普通消息先过规则版变化检测，无变化仅批内
累加 reinforcement，不调 LLM。text 事件是显式提交的素材，直通抽取（spec §10）。
"""
import re

_EXPLICIT_RE = re.compile(r"记住|以后|别再|不要|默认|总是|永远")
_LONG_MESSAGE = 300  # 长度突变阈值（字符）；超出视为"有变化"


def should_extract(event: dict, has_history: bool) -> tuple[bool, str]:
    """规则版变化检测：返回 (是否抽取, 命中规则)。

    (a) explicit 声明关键词命中；(b) feedback 非空；(c) 会话首查（has_history=False，
    管线以"用户已有画像条目"近似 session 首查，冷启动用户每条必抽）；
    (d) 消息长度突变（任一 user 消息 >300 字符）；其余 (e) False（不调抽取 LLM）。
    """
    for m in event.get("messages") or []:
        if m.get("role") != "user":
            continue
        text = m.get("text") or ""
        if _EXPLICIT_RE.search(text):
            return True, "explicit"
        if len(text) > _LONG_MESSAGE:
            return True, "long_message"
    if event.get("feedback"):
        return True, "feedback"
    if not has_history:
        return True, "first_query"
    return False, "no_change"
