"""事件 → 画像候选：对话/文本双模板抽取 + 指令/敏感安全防线。

Candidate 键固定：dimension/topic/scope/body/summary/confidence/kind/quote/topic_new，
orphan 候选额外带 status="orphan_topic"（dimension 占位 "orphan"）。
"""
import re

from mem.prompts import EXTRACT_CONVERSATION, EXTRACT_TEXT, INSTRUCTION_RE, MAX_BODY

# sensitive topic 概括级校验：含精确位置/医疗细节特征（数字门牌、楼栋、确诊、处方）→ 整条丢弃
_SENSITIVE_DETAIL_RE = re.compile(r"\d+号|\d+栋|确诊|处方")

_OUTPUT_SCHEMA = {
    "name": "extract_candidates",
    "schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {"type": ["string", "null"]},
                        "topic": {"type": "string"},
                        "scope": {"type": "string"},
                        "body": {"type": "string"},
                        "summary": {"type": "string"},
                        "confidence": {"type": "number"},
                        "kind": {"type": "string", "enum": ["explicit", "inferred", "feedback"]},
                        "quote": {"type": ["string", "null"]},
                        "topic_new": {"type": ["string", "null"]},
                    },
                },
            }
        },
        "required": ["candidates"],
    },
}


def extract(ev: dict, taxonomy, llm, known_ids: list[str]) -> list[dict]:
    """单事件抽取画像候选。conversation/text 走各自模板；逐条过滤后返回。"""
    block = _dimension_block(taxonomy)
    ids_line = ", ".join(known_ids) if known_ids else "（无）"
    if ev.get("event_type") == "text":
        content = ev.get("content", "")
        system = EXTRACT_TEXT.format(dimension_block=block, known_ids=ids_line, content=content)
        user = content
    else:
        system = EXTRACT_CONVERSATION.format(dimension_block=block, known_ids=ids_line)
        user = _render_messages(ev.get("messages", []))
    result = llm.chat_json(system, user, _OUTPUT_SCHEMA)
    out = []
    for raw in result.get("candidates", []):
        cand = _to_candidate(raw, taxonomy)
        if cand:
            out.append(cand)
    return out


def _dimension_block(taxonomy) -> str:
    return "\n".join(f"- {d.key}({d.name}): {', '.join(d.topics)}"
                     for d in taxonomy.dimensions.values())


def _render_messages(messages: list[dict]) -> str:
    return "\n".join(f"{m.get('role')}: {m.get('text', '')}" for m in messages)


def _is_sensitive(taxonomy, dimension: str, topic: str) -> bool:
    dim = taxonomy.dimensions.get(dimension)
    if not dim or not dim.topic_overrides:
        return False
    return bool(dim.topic_overrides.get(topic, {}).get("sensitive"))


def _to_candidate(raw: dict, taxonomy) -> dict | None:
    dimension = raw.get("dimension")
    topic_new = raw.get("topic_new")
    topic = raw.get("topic") or ""
    # null 或词表外（幻觉维度名）→ 一律走 orphan 路径，禁止发明类别
    if not dimension or dimension not in taxonomy.dimensions:
        if not topic_new:
            # null 路径: topic_new 必填（LLM 未填即丢弃）；
            # 幻觉维度路径: LLM 自认在词表内通常不填，由 原维度/主题 承接避免误丢
            topic_new = f"{dimension}/{topic}" if dimension and topic else None
        if not topic_new:
            return None
        dimension, status = "orphan", "orphan_topic"
    else:
        status, topic_new = None, None
    body = raw.get("body") or ""
    quote = raw.get("quote") or ""
    # 安全层 1: 指令类内容不是画像
    if INSTRUCTION_RE.search(body or quote):
        return None
    confidence = float(raw.get("confidence") or 0)
    # 安全层 2: sensitive topic → 置信上限 0.6 + 概括级校验（含精确位置/医疗细节即丢弃）
    if _is_sensitive(taxonomy, dimension, topic):
        confidence = min(confidence, 0.6)
        if _SENSITIVE_DETAIL_RE.search(f"{body} {quote}"):
            return None
    dim = taxonomy.dimensions.get(dimension)
    default_scope = dim.default_scope if dim else "global"
    cand = {
        "dimension": dimension,
        "topic": topic,
        "scope": raw.get("scope") or default_scope,
        "body": body[:MAX_BODY],
        "summary": raw.get("summary") or "",
        "confidence": confidence,
        "kind": raw.get("kind") or "explicit",
        "quote": quote or None,
        "topic_new": topic_new,
    }
    if status:
        cand["status"] = status
    return cand
