"""安全规则单一权威：指令检测/敏感细节/否定词/近似主题/别名归一。

正则与词表自 llm/prompts.py、serving/audit.py、pipeline/extractor.py 原样迁入
（行为不变）；extractor/audit/prompts 一律经本模块引用，禁止保留副本定义。
"""
import re

# 指令类内容（注入意图）→ 不是记忆条目。按意图分支，收窄泛化动词误杀面：
# 1) ignore 类：忽略/无视/disregard/ignore + (限定词)*(指令/提示/规则) 宾语
# 2) reveal 类：泄露/打印/复述... + 你的/系统提示/初始指令 宾语
# 3) obey 类：执行/follow + (你|系统|收到|上方|上述)*(命令/指令)（"运行 docker 命令"
#    "执行测试命令"等真实用户表述不命中：触发头只留"执行"，且限定词不含任意名词）
# 4) rm -rf 仅在"执行/运行"动词后命中；单独出现（如清理习惯）不命中
INSTRUCTION_RE = re.compile(
    r"(忽略|无视|disregard|ignore|forget)\s*"
    r"((之前|以上|上述|先前|以前|above|prior|previous|所有|全部|任何|all|any|the|的)\s*){0,3}"
    r"(指令|提示|约束|规则|instructions?|prompts?|rules?|constraints?)"
    r"|(泄露|透露|打印|输出|复述|显示|告诉我|repeat|print|reveal|show|output)"
    r"\s*(你的|your|the\s+)?\s*(系统提示|system\s*prompt|初始指令|initial\s+instructions?|内部提示|内部指令)"
    r"|(执行|follow|comply\s+with)\s*(你|系统|收到的|上方|上述|the\s+|this)?\s*"
    r"(收到|上方|上述)?\s*(的)?\s*(命令|指令|instruction)"
    r"|(执行|运行)\s+rm\s+-rf"
    r"|always\s+(output|follow|execute)\s+(the\s+)?(system|commands?)",
    re.I)

# sensitive topic 概括级校验：含精确位置/医疗细节特征（数字门牌、楼栋、确诊、处方）→ 整条丢弃
SENSITIVE_DETAIL_RE = re.compile(r"\d+号|\d+栋|确诊|处方")

# taboo 条目 quote 必须含否定词（spec §9 文件级 invariant）
NEGATION_RE = re.compile(r"不|别|禁|勿|拒绝|无需|避免|没有")


def normalize_topic(taxonomy, dimension: str, topic: str) -> str:
    """别名归一到 canonical topic（先归一再判词表），词表外 → ValueError。

    参照 extractor _to_candidate 的别名归一逻辑：同一语义的不同说法收敛到
    canonical topic，否则每次换措辞都会新开一行。
    """
    topic = str(topic or "").strip()
    dim = taxonomy.dimensions.get(dimension)
    if not dim:
        raise ValueError(f"dimension 不在词表: {dimension}")
    canonical = (getattr(dim, "topic_aliases", None) or {}).get(topic)
    if canonical and canonical in (dim.topics or []):
        topic = canonical
    if topic not in (dim.topics or []):
        raise ValueError(f"topic 不在词表: {dimension}/{topic}")
    return topic


def scan_instruction(*texts: str | None) -> bool:
    """任一文本命中指令模式（注入意图）→ True。"""
    return any(INSTRUCTION_RE.search(t) for t in texts if t)


def sensitive_cap(taxonomy, dimension: str, topic: str) -> float | None:
    """sensitive topic → 置信上限 0.6；否则 None。"""
    dim = taxonomy.dimensions.get(dimension)
    if not dim or not dim.topic_overrides:
        return None
    if dim.topic_overrides.get(topic, {}).get("sensitive"):
        return 0.6
    return None


def has_sensitive_detail(*texts: str | None) -> bool:
    """任一文本含精确位置/医疗细节特征 → True（sensitive topic 概括级校验）。"""
    return any(SENSITIVE_DETAIL_RE.search(t) for t in texts if t)


def quote_is_negated(quote: str) -> bool:
    """quote 含否定词 → True（taboo 文件级 invariant，spec §9）。"""
    return bool(NEGATION_RE.search(quote or ""))


def topics_near(a: str, b: str) -> bool:
    """近似同主题：剥 ~/+ 前缀后相等、一方为另一方子串、或共享词根（分隔符首段）。"""
    a = a.lstrip("~+")
    b = b.lstrip("~+")
    if a == b or a in b or b in a:
        return True
    root = lambda t: re.split(r"[\W_]+", t)[0]  # noqa: E731 首段词根
    return bool(root(a)) and root(a) == root(b)
