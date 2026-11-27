"""触发分级（spec §10 成本控制核心，仅作用于 dialogue）：

explicit 声明与显式 force_extract 零阈值直通；普通消息先过规则版变化检测，无变化仅批内
累加 reinforcement，不调 LLM。document 事件是显式提交的素材，直通抽取（spec §10）。
"""
import re

from pismor.pipeline.normalize import NormalizedEvent

# 默认词表与 settings.pipeline.trigger_explicit_keywords 保持一致（中英文声明关键词）。
_DEFAULT_EXPLICIT_KEYWORDS = (
    "记住", "以后", "别再", "不要", "默认", "总是", "永远",
    "remember", "remember that", "from now on", "always", "never",
    "stop", "don't", "do not", "prefer", "keep in mind", "going forward",
)


def build_explicit_re(keywords) -> re.Pattern:
    """由词表编译 explicit 命中正则；词表按长度降序拼接，避免短词抢先命中。

    英文词做词边界包裹（避免 "stopwatch" 命中 "stop"）并忽略大小写（"Remember"）；
    中文词直接拼接。
    """
    kws = sorted((k for k in keywords if k), key=len, reverse=True)
    en = [re.escape(k) for k in kws if k.isascii()]
    zh = [re.escape(k) for k in kws if not k.isascii()]
    parts = [rf"\b{k}\b" for k in en] + zh
    return (re.compile("|".join(parts), re.IGNORECASE) if parts
            else re.compile(r"(?!)"))  # 空词表 → 永不命中


_EXPLICIT_RE = build_explicit_re(_DEFAULT_EXPLICIT_KEYWORDS)


def should_extract(ev: NormalizedEvent, has_history: bool, *, long_message: int = 300,
                   explicit_re: re.Pattern | None = None) -> tuple[bool, str]:
    """规则版变化检测：返回 (是否抽取, 命中规则)。

    (a) explicit 声明关键词命中（词表可配，settings.pipeline.trigger_explicit_keywords）；
    (b) 消息长度突变（任一 user 消息 >long_message 字符，settings.pipeline 可配）；
    (c) force_extract 为真（入口 schema 已保证存在 user 文本，正常调用不会走到 no-op）；
    (d) 会话首查（has_history=False，管线以"用户已有记忆条目"近似，冷启动用户每条必抽）；
    其余 (e) False（不调抽取 LLM）。
    """
    ere = explicit_re or _EXPLICIT_RE
    for text in ev.user_texts:
        if ere.search(text):
            return True, "explicit"
        if len(text) > long_message:
            return True, "long_message"
    if ev.force_extract and ev.user_texts:
        return True, "forced"
    if not has_history:
        return True, "first_query"
    return False, "no_change"
