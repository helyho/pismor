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
t9() {
  need_llm || return 2
  local ev; ev=$(next_event_id t9e)
  http POST /events "$(bb_event t9u "$ev" "别再给我生成 Excel 公式了, 用 Python")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t9u' AND e.dimension='taboo'" 90 \
    || { case_fail "90s 内 taboo 条目未入库"; return 1; }
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
    || { case_fail "90s 内 identity 条目未入库"; return 1; }
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
    || { case_fail "90s 内条目未入库"; return 1; }
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
  wait_db "SELECT count(*) FROM entries e WHERE e.user_id='t12u' AND e.dimension IS NOT NULL" 120 \
    || { case_fail "120s 内两条均未入库"; }
  http GET "/profile/bb/t12u?query=python"
  expect_status 200 || return 1
  expect_json '.entries | length > 0' 'true' || return 1
  expect_json '[.entries[] | select((tojson | test("python|lang"; "i")))] | length > 0' 'true' \
    || case_fail "query=python 未召回落 Python 相关条目"
  case_pass
}
run_domain_assemble() {
  run_case T8 "空包合法" -- t8
  run_case T9 "taboo入包置顶" --retry -- t9
  run_case T10 "identity入包" --retry -- t10
  run_case T11 "用户隔离" --retry -- t11
  run_case T12 "query召回" --retry -- t12
}
