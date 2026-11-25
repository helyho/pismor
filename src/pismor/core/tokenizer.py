"""BM25 词法索引（mem_fts）的分词器：写入与查询必须同源（同一配置）。

- "jieba"（默认）：jieba.cut_for_search 搜索引擎粒度，语义分词，BM25 TF/IDF 有效；
- "bigram"：汉字取相邻二字组、英文/数字取整词，零分词依赖（原 assembler.tokens 逻辑）。

硬约束：切换 tokenizer 配置后，mem_fts 索引内是旧分词产物 → 必须重建
（init_schema 检测 fts_meta 不符自动 rebuild；也可 scripts/rebuild_fts.py 手动重建）。
索引侧不去重不截断（BM25 的 TF 依赖词频）；去重/截断是查询侧（assembler.tokens）的职责。
"""
import re

_TOKEN_RE = re.compile(r"[0-9A-Za-z_]{2,}|[\u4e00-\u9fff]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
# jieba 输出过滤：token 必须含字母/数字/汉字（剔除空白、标点、纯符号）
_JIEBA_KEEP_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]")
_ASCII_RE = re.compile(r"[0-9A-Za-z_]+")

_current = "jieba"
_VALID = ("jieba", "bigram")


def set_tokenizer(name: str) -> None:
    """进程级切换分词器（api/scripts 启动时按 settings.assembler.tokenizer 调用）。"""
    if name not in _VALID:
        raise ValueError(f"unknown tokenizer: {name!r} (valid: {_VALID})")
    global _current
    _current = name


def current() -> str:
    return _current


def _bigram_tokens(text: str) -> list[str]:
    out = []
    for seg in _TOKEN_RE.findall(text or ""):
        if _HAN_RE.match(seg):
            out.append(seg) if len(seg) <= 2 else \
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            out.append(seg.lower())
    return out


def _jieba_tokens(text: str) -> list[str]:
    # 延迟导入：jieba 首次加载需构建词典（约 1s），仅在 jieba 路径付出
    import jieba
    out = []
    for t in jieba.cut_for_search(text or ""):
        t = t.strip()
        if not t or not _JIEBA_KEEP_RE.search(t):
            continue
        out.append(t.lower() if _ASCII_RE.fullmatch(t) else t)
    return out


def tokenize(text: str) -> list[str]:
    """全文/查询统一切词：索引侧与查询侧同用（同源硬约束）。

    与查询侧的差异职责：去重、截断 32 由 assembler.tokens 做（仅查询需要）。
    """
    if _current == "jieba":
        return _jieba_tokens(text)
    return _bigram_tokens(text)
