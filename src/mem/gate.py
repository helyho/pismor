"""置信度闸门：auto(≥auto_min 且 explicit) / review(review_min~auto_min 或 inferred 压顶后) / dropped(<review_min)。

inferred 一律 cap（默认 0.8，即使原始置信更高也不得自动入库）。
force_review（如 taboo 词表外候选）无视置信直入人工队列（spec §7 禁止自动建桶）。
阈值可由 settings.gate 配置（GateSettings）。
"""


def gate(candidates, *, auto_min=0.85, review_min=0.6, inferred_cap=0.8):
    auto, review, dropped = [], [], []
    for c in candidates:
        if c.get("force_review"):
            review.append(c)
            continue
        if c.get("kind") == "inferred":
            c["confidence"] = min(c["confidence"], inferred_cap)
        if c["confidence"] >= auto_min and c.get("kind") == "explicit":
            auto.append(c)
        elif c["confidence"] >= review_min:
            review.append(c)
        else:
            dropped.append(c)
    return auto, review, dropped
