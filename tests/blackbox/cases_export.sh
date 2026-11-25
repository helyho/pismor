#!/usr/bin/env bash
# 域4 导出与删除权（T17-T20）：导出生成/确定性、删除全链、tombstone 持续
# event_id 一律经 next_event_id 生成：retry 重跑拿新 id，不撞产品幂等去重
# T17 导出生成（LLM）：root 非空、目录含 .md、内容含已入库主题词
t17() {
  need_llm || return 2
  local ev; ev=$(next_event_id t17e)
  http POST /events "$(bb_event t17u "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t17u' AND e.dimension='preference' AND e.topic='lang'" 90 \
    || { case_fail "90s 内条目未入库"; }
  http GET /export/bb/t17u
  expect_status 200 || return 1
  expect_json '.root | length > 0' 'true' || return 1
  local root
  root=$(printf '%s' "$BODY" | jq -r '.root')
  [[ -d "$root" ]] || { case_fail "导出目录不存在: $root"; }
  local md
  md=$(find "$root" -type f -name '*.md' 2>/dev/null | head -1)
  [[ -n "$md" ]] || { case_fail "导出目录无 .md 文件: $root"; }
  grep -qi "python" "$root"/*.md 2>/dev/null || { case_fail "导出内容未含已入库主题词 python"; }
  case_pass
}
# T18 导出确定性（LLM）：同用户连续两次导出，.md 内容一致（按路径排序拼接，避免 find 顺序漂移）
t18() {
  need_llm || return 2
  local ev; ev=$(next_event_id t18e)
  http POST /events "$(bb_event t18u "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t18u'" 90 \
    || { case_fail "90s 内条目未入库"; }
  local h1 h2 root
  http GET /export/bb/t18u
  expect_status 200 || return 1
  root=$(printf '%s' "$BODY" | jq -r '.root')
  h1=$(find "$root" -type f -name '*.md' 2>/dev/null | sort | xargs -I@ cat "@" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
  [[ -n "$h1" ]] || { case_fail "第一次导出无 .md 内容"; }
  http GET /export/bb/t18u
  expect_status 200 || return 1
  root=$(printf '%s' "$BODY" | jq -r '.root')
  h2=$(find "$root" -type f -name '*.md' 2>/dev/null | sort | xargs -I@ cat "@" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
  [[ "$h1" == "$h2" ]] || { case_fail "两次导出内容不一致 ($h1 vs $h2)"; }
  case_pass
}
# T19 删除权全链（LLM 自建数据）：tombstones 留痕、关联表清空、导出删除、profile 空包
t19() {
  need_llm || return 2
  local ev; ev=$(next_event_id t19e)
  http POST /events "$(bb_event t19u "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t19u'" 90 \
    || { case_fail "90s 内条目未入库"; }
  local ids root19
  ids=$(db "SELECT group_concat(id) FROM entries WHERE user_source='bb' AND user_id='t19u'")
  [[ -n "$ids" ]] || { case_fail "未取到条目 id，删除链无数据可验"; }
  http GET /export/bb/t19u
  expect_status 200 || return 1
  root19=$(printf '%s' "$BODY" | jq -r '.root')
  http DELETE /profile/bb/t19u
  expect_status 200 || return 1
  expect_json_true '.deleted' || return 1
  expect_db "SELECT count(*) FROM tombstones WHERE user_source='bb' AND user_id='t19u' AND requested_by='api'" "1" || return 1
  expect_db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  expect_db "SELECT count(*) FROM sources WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  expect_db "SELECT count(*) FROM review_history WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  expect_db "SELECT count(*) FROM pending_clarifications WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  [[ ! -e "$root19" ]] || { case_fail "导出目录未删除: $root19"; }
  http GET /profile/bb/t19u
  expect_status 200 || return 1
  expect_json '.entries' '[]' || return 1
  case_pass
}
# T20 tombstone 持续（LLM）：删后含触发词事件不得复活画像
t20() {
  need_llm || return 2
  local e1 e2
  e1=$(next_event_id t20e)
  e2=$(next_event_id t20e2)
  http POST /events "$(bb_event t20u "$e1" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t20u'" 90 \
    || { case_fail "90s 内条目未入库"; }
  http DELETE /profile/bb/t20u
  expect_status 200 || return 1
  expect_json_true '.deleted' || return 1
  http POST /events "$(bb_event t20u "$e2" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM events_seen WHERE event_id='$e2'" 30 || { case_fail "删后事件未被接受"; }
  sleep 45 # 后台管线处理时延
  # 产品语义：tombstone 在装配层屏蔽（assembler 查 tombstones → 空包），
  # 后台仍可能重写 entries 行；因此断言"删除后画像不得装配"而非 entries=0
  http GET /profile/bb/t20u
  expect_status 200 || return 1
  expect_json '.entries' '[]' || return 1
  case_pass
}
run_domain_export() {
  run_case T17 "导出生成" --retry -- t17
  run_case T18 "导出确定性" -- t18
  run_case T19 "删除权全链" -- t19
  run_case T20 "tombstone持续" --retry -- t20
}
