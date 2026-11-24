"""事件 → 画像候选：对话/文本双模板抽取 + 指令/敏感安全防线 + 澄清回答规范化。

Candidate 键固定：dimension/topic/scope/body/summary/confidence/kind/quote/topic_new，
orphan 候选额外带 status="orphan_topic"（dimension 占位 "orphan"）。

词表硬约束（spec §7/§8）：
- dimension 合法但 topic ∉ 该维度 topics → 非 taboo 转 orphan（topic_new 必填，topic 用
  topic_new 作观察区键）；taboo 维度词表外候选强制人工队列（force_review），不得自动建桶。
- summary 在出口截断到 20 字（spec §5）。
- scope 非法（白名单外自由文本）在出口回落 default_scope 并置信打 8 折，不再由写入器
  整条丢弃——画像不因 LLM 的格式方差静默消失（黑盒发现⑤的修复）。
"""
import logging
import re

from mem.keys import valid_scope
from mem.prompts import (CLARIFY_EXTRACT_SYSTEM, CLARIFY_EXTRACT_USER,
                         EXTRACT_CONVERSATION, EXTRACT_TEXT, INSTRUCTION_RE, MAX_BODY)

logger = logging.getLogger("mem.extractor")

# sensitive topic 概括级校验：含精确位置/医疗细节特征（数字门牌、楼栋、确诊、处方）→ 整条丢弃
_SENSITIVE_DETAIL_RE = re.compile(r"\d+号|\d+栋|确诊|处方")

_CANDIDATE_ITEM = {
    "type": "object",
    "properties": {
        "dimension": {"type": ["string", "null"]},
        "topic": {"type": "string"},
        "scope": {"type": "string", "pattern": "^(global|project:.+)$"},
        "body": {"type": "string"},
        "summary": {"type": "string"},
        "confidence": {"type": "number"},
        "kind": {"type": "string", "enum": ["explicit", "inferred", "feedback"]},
        "quote": {"type": ["string", "null"]},
        "topic_new": {"type": ["string", "null"]},
    },
}


def _schema(name: str) -> dict:
    return {"name": name, "schema": {
        "type": "object",
        "properties": {"candidates": {"type": "array", "items": _CANDIDATE_ITEM}},
        "required": ["candidates"],
    }}


_OUTPUT_SCHEMA = _schema("extract_candidates")
_CLARIFY_SCHEMA = _schema("clarify_candidates")


def extract(ev: dict, taxonomy, llm, known_ids: list[str], *, max_body: int = MAX_BODY) -> list[dict]:
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
        cand = _to_candidate(raw, taxonomy, max_body=max_body)
        if cand:
            out.append(cand)
    return out


def extract_clarification(answer: str, taxonomy, llm, pair_rows, *,
                          max_body: int = MAX_BODY) -> list[dict]:
    """把澄清回答规范化为画像候选（body/summary 来自抽取，不直接使用用户原句）。

    pair_rows 为矛盾对现存行（dimension/topic/scope/summary），仅作理解上下文。
    输出候选 kind 统一为 clarification（证据类别，spec §5/§11），scope 可拆分。
    """
    pairs = "\n".join(
        f"- {r['dimension']}/{r['topic']}@{r['scope']}：{r['summary']}" for r in pair_rows
    ) or "（无）"
    system = CLARIFY_EXTRACT_SYSTEM.format(dimension_block=_dimension_block(taxonomy), pairs=pairs)
    result = llm.chat_json(system, CLARIFY_EXTRACT_USER.format(answer=answer),
                           _CLARIFY_SCHEMA)
    out = []
    for raw in result.get("candidates", []):
        cand = _to_candidate(raw, taxonomy, max_body=max_body)
        if not cand or cand["dimension"] == "orphan" or cand.get("status") == "orphan_topic":
            continue  # 澄清回答只收敛既有矛盾主题，不新建观察区桶
        if not cand.get("quote"):
            cand["quote"] = answer
        cand["kind"] = "clarification"
        cand["confidence"] = max(float(cand.get("confidence") or 0), 0.85)
        out.append(cand)
    return out


def _dimension_block(taxonomy) -> str:
    """维度词表 + 判定要点（guidance 压缩注入）。

    黑盒发现①的修复：此前只注入 topic 列表，taxonomy 的 guidance（kind/scope/
    维度边界判定语义）完全没进 prompt，LLM 对 kind/scope/topic 只能靠猜。
    """
    lines = []
    for d in taxonomy.dimensions.values():
        guidance = re.sub(r"\s+", " ", (d.guidance or "")).strip()
        if len(guidance) > 200:
            guidance = guidance[:200] + "…"
        lines.append(f"- {d.key}({d.name}): topics={', '.join(d.topics)}"
                     + (f"；判定要点：{guidance}" if guidance else ""))
    return "\n".join(lines)


def _render_messages(messages: list[dict]) -> str:
    return "\n".join(f"{m.get('role')}: {m.get('text', '')}" for m in messages)


def _is_sensitive(taxonomy, dimension: str, topic: str) -> bool:
    dim = taxonomy.dimensions.get(dimension)
    if not dim or not dim.topic_overrides:
        return False
    return bool(dim.topic_overrides.get(topic, {}).get("sensitive"))


def _to_candidate(raw: dict, taxonomy, *, max_body: int = MAX_BODY) -> dict | None:
    dimension = raw.get("dimension")
    topic_new = raw.get("topic_new")
    topic = str(raw.get("topic") or "").strip()
    force_review = False
    # 1) dimension 不在词表 → orphan（禁止发明类别）
    if not dimension or dimension not in taxonomy.dimensions:
        if not topic_new:
            # null 路径: topic_new 必填（LLM 未填即丢弃）；
            # 幻觉维度路径: LLM 自认在词表内通常不填，由 原维度/主题 承接避免误丢
            topic_new = f"{dimension}/{topic}" if dimension and topic else None
        if not topic_new:
            return None
        dimension, status = "orphan", "orphan_topic"
        topic = topic_new
    else:
        dim = taxonomy.dimensions[dimension]
        if topic not in (dim.topics or []):
            # 2) 合法维度 + 词表外 topic（spec §7 禁止发明主题词）
            if dimension == "taboo":
                # taboo 词表禁止运行期扩充：保留维度进人工队列，不自动建桶
                topic = topic or (topic_new or "unclassified")
                topic_new = None  # 非 orphan 行不得带 topic_new（审计字段互斥）
                status = None
                force_review = True
            else:
                topic_new = topic_new or (f"{dimension}/{topic}" if topic else None)
                if not topic_new:
                    return None
                dimension, status = "orphan", "orphan_topic"
                topic = topic_new  # 观察区键用 topic_new（同主题复现复用一行）
        else:
            status, topic_new = None, None
    body = str(raw.get("body") or "")
    quote = str(raw.get("quote") or "")
    summary = str(raw.get("summary") or "")
    # 安全层 1: 指令类内容不是画像（body/quote/summary 同扫）
    if INSTRUCTION_RE.search(f"{body}\n{quote}\n{summary}"):
        return None
    confidence = float(raw.get("confidence") or 0)
    # 安全层 2: sensitive topic → 置信上限 0.6 + 概括级校验（含精确位置/医疗细节即丢弃）
    if _is_sensitive(taxonomy, dimension, topic):
        confidence = min(confidence, 0.6)
        if _SENSITIVE_DETAIL_RE.search(f"{body} {quote}"):
            return None
    dim = taxonomy.dimensions.get(dimension)
    default_scope = dim.default_scope if dim else "global"
    scope = str(raw.get("scope") or "").strip()
    if not valid_scope(scope):
        # 兜底（替代下游整条丢弃）：非法 scope 回落默认作用域并降置信，日志留痕
        logger.warning("scope %r out of whitelist; fallback to %s for %s/%s",
                       raw.get("scope"), default_scope, dimension, topic)
        scope = default_scope
        confidence *= 0.8
    cand = {
        "dimension": dimension,
        "topic": topic,
        "scope": scope,
        "body": body[:max_body],
        "summary": summary[:20],  # spec §5：摘要硬顶 20 字
        "confidence": confidence,
        "kind": raw.get("kind") or "explicit",
        "quote": quote or None,
        "topic_new": topic_new,
    }
    if status:
        cand["status"] = status
    if force_review:
        cand["force_review"] = True
    return cand
