"""置信度闸门：auto(≥0.85 且 explicit) / review(0.6~0.85 或 inferred 压顶后) / dropped(<0.6)。

inferred 一律 cap 0.8（即使原始置信更高也不得自动入库）。
"""


def gate(candidates):
    auto, review, dropped = [], [], []
    for c in candidates:
        if c.get("kind") == "inferred":
            c["confidence"] = min(c["confidence"], 0.8)
        if c["confidence"] >= 0.85 and c.get("kind") == "explicit":
            auto.append(c)
        elif c["confidence"] >= 0.6:
            review.append(c)
        else:
            dropped.append(c)
    return auto, review, dropped
