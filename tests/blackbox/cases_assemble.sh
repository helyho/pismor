#!/usr/bin/env bash
# 域2 装配（T8-T12）：GET /profile 包结构、维度入包、用户隔离、query 召回
# event_id 一律经 next_event_id 生成：retry 重跑拿新 id，不撞产品幂等去重
# T8 空包合法：结构五键齐全，永不 5xx
t8() {
  http GET /profile/bb/nobody
  expect_status 200 || return 1
  expect_json '.entries' '[]' || return 1
  local k
  for k in entries budget_used clarification_needed merge_task rendered; do
    expect_json "has(\"$k\")" 'true' || return 1
  done
  case_pass
}
# T9 taboo 入包（LLM）：taxonomy 回归样例，taboo 排包首位
# 响应条目只有 {id,text,confidence,status}，维度经 id 前缀（dimension_topic）识别
t9() {
  need_llm || return 2
  local ev; ev=$(next_event_id t9e)
  http POST /events "$(bb_event t9u "$ev" "别再给我生成 Excel 公式了, 用 Python")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t9u' AND e.dimension='taboo'" 90 \
    || { case_fail "90s 内 taboo 条目未入库"; }
  promote_active t9u taboo || return 1
  http GET /profile/bb/t9u
  expect_status 200 || return 1
  expect_json '[.entries[] | select(.id | startswith("taboo"))] | length > 0' 'true' || return 1
  expect_json '.entries[0].id | startswith("taboo")' 'true' || return 1
  case_pass
}
# T10 identity 入包（LLM）
t10() {
  need_llm || return 2
  local ev; ev=$(next_event_id t10e)
  http POST /events "$(bb_event t10u "$ev" "我是一名后端工程师")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t10u' AND e.dimension='identity'" 90 \
    || { case_fail "90s 内 identity 条目未入库"; }
  promote_active t10u identity || return 1
  http GET /profile/bb/t10u
  expect_status 200 || return 1
  expect_json '[.entries[] | select(.id | startswith("identity"))] | length > 0' 'true' || return 1
  case_pass
}
# T11 用户隔离：u 有画像后，其他 user / 其他 source 均为空包
t11() {
  need_llm || return 2
  local ev; ev=$(next_event_id t11e)
  http POST /events "$(bb_event t11u "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t11u' AND e.dimension='preference'" 90 \
    || { case_fail "90s 内条目未入库"; }
  # 无 query 的包只含必带集(taboo+identity)；preference 经 query 召回验证在包
  http GET "/profile/bb/t11u?query=python"
  expect_status 200 || return 1
  expect_json '.entries | length > 0' 'true' || return 1
  http GET /profile/bb/t11other
  expect_status 200 || return 1
  expect_json '.entries' '[]' || return 1
  http GET /profile/app/t11u
  expect_status 200 || return 1
  expect_json '.entries' '[]' || return 1
  case_pass
}
# T12 query 召回（LLM）：相关条目在包内，性质级不断言排序
t12() {
  need_llm || return 2
  local e1 e2
  e1=$(next_event_id t12a)
  e2=$(next_event_id t12b)
  http POST /events "$(bb_event t12u "$e1" "记住, 我日常写脚本都用 Python")"
  expect_status 202 || return 1
  http POST /events "$(bb_event t12u "$e2" "记住, 我周末喜欢爬山")"
  expect_status 202 || return 1
  # 等 Python 事件真正落库：sources 行由 origin_ref 唯一确定，不依赖 LLM 归类。
  # 若只等"任意条目入库"，爬山事件先落库时会早退，promote 发生在 Python 条目写入前，
  # 该条目随后以 pending_review 入库、装配取不到 → 历史间歇 FAIL 的根因。
  wait_db "SELECT 1 FROM sources WHERE user_source='bb' AND user_id='t12u' AND origin_ref='blackbox:$e1'" 120 \
    || { case_fail "120s 内 Python 事件未落库"; }
  # 装配只取 active/degraded；LLM 置信度随机，条目可能停在 pending_review。
  local i
  for i in 1 2 3; do
    promote_all t12u || { case_fail "promote_all 失败"; }
    wait_db "SELECT 1 FROM entries e WHERE e.user_source='bb' AND e.user_id='t12u' AND e.status='active'" 15 \
      && break
  done
  http GET "/profile/bb/t12u?query=python"
  expect_status 200 || return 1
  expect_json '.entries | length > 0' 'true' || return 1
  # 命中条目正文（.text）而非 id——id 形如 preference_lang 会自命中"lang"造成同义反复
  expect_json '[.entries[] | select(.text | test("python"; "i"))] | length > 0' 'true' \
    || { case_fail "query=python 未召回正文含 Python 的条目"; }
  case_pass
}
run_domain_assemble() {
  run_case T8 "空包合法" -- t8
  run_case T9 "taboo入包置顶" --retry -- t9
  run_case T10 "identity入包" --retry -- t10
  run_case T11 "用户隔离" --retry -- t11
  run_case T12 "query召回" --retry -- t12
}
