"""回归样例脚本：taxonomy.regression_cases → text 事件 → extract → expect 断言。

用法：
    python scripts/run_regression.py --settings config/settings.yaml   # 真实 LLM
    python scripts/run_regression.py --dry                             # FakeLLM 回放 expect，自检脚本

判定口径：
- expect.dimension 为 null：断言零候选（引用/转述、注入类内容必须拒绝入库）；
- expect.confidence_below：断言匹配候选置信 ≤ 边界且闸门后落 review 桶
  （review 下界 0.6，边界值 0.6 恰入 review，故按 ≤ 判定）；
- 其余：断言 dimension/topic/kind（有 scope 时一并断言）匹配。
输出 `passed x/y`；非全绿 exit 1。
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from pismor.pipeline import extractor, gate  # noqa: E402
from pismor.core.config import load_settings  # noqa: E402
from pismor.llm.client import LLMClient  # noqa: E402
from pismor.core.taxonomy import load_taxonomy  # noqa: E402


class ExpectReplayLLM:
    """--dry 用：把 expect 反构为抽取响应回放，验证脚本自身的判定管线。"""

    def __init__(self, case: dict):
        self.case = case
        self.calls = 0

    def chat_json(self, system, user, schema):
        self.calls += 1
        expect = self.case["expect"]
        if expect.get("dimension") is None:
            return {"candidates": []}
        quote = self.case["quote"]
        return {"candidates": [{
            "dimension": expect["dimension"],
            "topic": expect.get("topic") or "",
            "scope": expect.get("scope") or "global",
            "body": quote,
            "summary": quote[:20],
            "confidence": expect.get("confidence_below", 0.92),
            "kind": expect.get("kind") or "explicit",
            "quote": quote,
            "topic_new": None,
        }]}


def _text_event(case: dict, index: int) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"source": "regression", "user_ref": f"case_{index}",
            "event_id": f"regression_{index}", "event_type": "text",
            "content": case["quote"], "ts": now, "origin_ref": f"taxonomy:{index}"}


def judge(case: dict, candidates: list[dict]) -> tuple[bool, str]:
    expect = case["expect"]
    if expect.get("dimension") is None:
        if candidates:
            return False, f"期望零候选，实际 {len(candidates)} 条"
        return True, "零候选"

    matched = [c for c in candidates
               if c["dimension"] == expect["dimension"]
               and ("topic" not in expect or c["topic"] == expect["topic"])
               and ("scope" not in expect or c["scope"] == expect["scope"])
               and ("kind" not in expect or c["kind"] == expect["kind"])]
    if not matched:
        return False, f"无匹配候选（实际 {[(c['dimension'], c['topic'], c['kind']) for c in candidates]}）"

    if "confidence_below" in expect:
        limit = expect["confidence_below"]
        cand = matched[0]
        if cand["confidence"] > limit:
            return False, f"置信 {cand['confidence']} 未低于边界 {limit}"
        _auto, review, _dropped = gate.gate([cand])
        if cand not in review:
            return False, f"置信 {cand['confidence']} 闸门后未进 review"
        return True, f"置信 {cand['confidence']} → review"
    return True, "字段匹配"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--dry", action="store_true", help="用 FakeLLM 回放 expect 自检脚本")
    args = parser.parse_args(argv)

    settings = None
    if args.dry:
        settings_path = args.settings if args.settings.exists() else None
        taxonomy_path = (load_settings(settings_path).taxonomy_path if settings_path
                         else _REPO / "config" / "taxonomy.yaml")
    else:
        if not args.settings.exists():
            parser.error(f"settings not found: {args.settings}")
        settings = load_settings(args.settings)
        taxonomy_path = settings.taxonomy_path

    taxonomy = load_taxonomy(taxonomy_path)
    llm = None if args.dry else LLMClient(settings.llm)

    total = len(taxonomy.regression_cases)
    passed = 0
    for i, case in enumerate(taxonomy.regression_cases, 1):
        case_llm = ExpectReplayLLM(case) if args.dry else llm
        try:
            candidates = extractor.extract(_text_event(case, i), taxonomy, case_llm,
                                           known_ids=[])
        except Exception as exc:  # 单例失败不影响其余样例，整体仍非全绿
            print(f"[FAIL] {i}. {case['quote']} — 抽取异常: {exc}")
            continue
        ok, reason = judge(case, candidates)
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {i}. {case['quote']} — {reason}")

    print(f"passed {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
