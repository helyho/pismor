#!/usr/bin/env bash
# 域3 生命周期/复核（T14-T18）：矛盾收敛、decision 流转、复核队列、长消息触发、澄清回流
# event_id 一律经 next_event_id 生成：retry 重跑拿新 id，不撞产品幂等去重
# T14 矛盾收敛（LLM）：两条 lang 主题矛盾指令，终态生效条目至多 1 条
t14() {
  need_llm || return 2
  local e1 e2
  e1=$(next_event_id t13a)
  e2=$(next_event_id t13b)
  http POST /events "$(bb_event t13u "$e1" "以后都用 Python 3.12 写脚本")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t13u'" 120 \
    || { case_fail "第一条 120s 未入库"; }
  http POST /events "$(bb_event t13u "$e2" "以后别用 Python 了, 改用 Rust")"
  expect_status 202 || return 1
  # 第二条的管线完成信号用 sources 行（extract 落库），而不是 events_seen（同步写、不代表处理完）
  if ! wait_db "SELECT 1 FROM sources WHERE user_source='bb' AND user_id='t13u' AND origin_ref='blackbox:$e2'" 120; then
    sleep 20 # 第二条可能被判重复/免提问：给收敛留时间后仍按性质断言
  fi
  # 轮询收敛（最多 60s）：active lang 条目至多 1 条
  local waited=0 n=99
  while ((waited < 60)); do
    n=$(db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='t13u' AND topic='lang' AND status='active'")
    [[ "$n" -le 1 ]] && break
    sleep 2
    waited=$((waited + 2))
  done
  [[ "$n" -le 1 ]] || { case_fail "lang 主题 active 条目 ${n} 条，矛盾未收敛"; }
  http GET /memory/bb/t13u
  expect_status 200 || return 1
  case_pass
}
# T15 decision 流转（LLM 建数据，流转本身确定）：confirm→旧版本 noop→remove 行消失
# user 每次 attempt 唯一（retry 重跑不污染 review_history 计数）
t15() {
  need_llm || return 2
  local uid="t14u${RANDOM}" ev; ev=$(next_event_id t14e)
  http POST /events "$(bb_event "$uid" "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_source='bb' AND e.user_id='${uid}' AND e.dimension='preference' AND e.topic='lang'" 120 \
    || { case_fail "120s 内条目未入库"; }
  local entry_id ver ver2
  entry_id=$(db "SELECT id FROM entries WHERE user_source='bb' AND user_id='${uid}' AND topic='lang' ORDER BY rowid DESC LIMIT 1")
  [[ -n "$entry_id" ]] || { case_fail "未取到 entry_id"; }
  ver=$(db "SELECT updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}' ORDER BY rowid DESC LIMIT 1")
  [[ -n "$ver" ]] || { case_fail "未取到 updated_at"; }
  local vpath="/review/bb/${uid}/${entry_id}/decision"
  # updated_at 是秒级：confirm 前跨一秒，保证版本推进，"旧版本重发"才有 noop 语义
  sleep 1
  http POST "$vpath" "$(jq -n --arg decision confirm --arg v "$ver" '{decision:$decision, entry_version:$v}')"
  expect_status 200 || return 1
  expect_json '.noop' 'false' || return 1
  expect_db "SELECT count(*) FROM review_history WHERE user_source='bb' AND user_id='${uid}' AND entry_id='${entry_id}'" "1" || return 1
  # 旧版本重发 → noop（版本失配保护）
  http POST "$vpath" "$(jq -n --arg decision remove --arg v "$ver" '{decision:$decision, entry_version:$v}')"
  expect_status 200 || return 1
  expect_json_true '.noop' || return 1
  # 新版本 remove → 该条目消失
  ver2=$(db "SELECT updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}' ORDER BY rowid DESC LIMIT 1")
  [[ -n "$ver2" ]] || { case_fail "remove 前 entry 已消失"; }
  if [[ "$ver2" == "$ver" ]]; then
    sleep 1
    ver2=$(db "SELECT updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}' ORDER BY rowid DESC LIMIT 1")
  fi
  [[ "$ver2" != "$ver" ]] || { case_fail "confirm 后 updated_at 未推进（版本轮转失效）"; }
  http POST "$vpath" "$(jq -n --arg decision remove --arg v "$ver2" '{decision:$decision, entry_version:$v}')"
  expect_status 200 || return 1
  expect_json '.noop' 'false' || return 1
  expect_db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}'" "0" || return 1
  case_pass
}
# T16 复核队列结构：200、items 为数组、非空时键齐全
t16() {
  http GET /review/queue
  expect_status 200 || return 1
  expect_json '.items | type' 'array' || return 1
  local k
  for k in entry_id status entry_version; do
    expect_json "[.items[] | select(has(\"$k\") | not)] | length" '0' || return 1
  done
  case_pass
}
# T17 长消息触发（LLM）：>300 字符无触发词纯文本，管线后入库（性质级）
t17() {
  need_llm || return 2
  local long ev
  long=$(printf '我们团队的工作流是这样的, 需求评审之后进入开发, 开发完成提交代码评审, 评审通过再合入主干. 日常站会十五分钟, 迭代周期两周. 发布流程走灰度, 先百分之五再全量. 监控告警接企微, 线上问题四小时内响应. 文档随代码一起维护, 接口变更同步更新 openapi 描述. %.0s' $(seq 1 5))
  ev=$(next_event_id t16e)
  http POST /events "$(bb_event t16u "$ev" "$long")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t16u'" 120 \
    || { case_fail "长消息 120s 内未入库"; }
  case_pass
}
# T18 澄清回流闭环（LLM）：互斥偏好双存 → 装配提问 → 对话回答 → pending 消费、冲突消解
t18_clarify() {
  need_llm || return 2
  local uid="t18u${RANDOM}" e1 e2 e3 n waited=0
  e1=$(next_event_id t18a)
  e2=$(next_event_id t18b)
  e3=$(next_event_id t18c)

  # 1) 两条同 key 互斥偏好（preference/verbosity）：简短 vs 详细 → 双存 degraded
  http POST /events "$(bb_event "$uid" "$e1" "以后回答我都简短一点")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM sources WHERE user_source='bb' AND user_id='$uid' AND origin_ref='blackbox:$e1'" 120 \
    || { case_fail "第一条未完成"; }
  http POST /events "$(bb_event "$uid" "$e2" "以后回答我都详细一点")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM sources WHERE user_source='bb' AND user_id='$uid' AND origin_ref='blackbox:$e2'" 120 \
    || { case_fail "第二条未完成"; }

  # 2) 矛盾双方 degraded 且 conflict_with 互指
  n=$(db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='$uid' AND status='degraded' AND conflict_with IS NOT NULL")
  [[ "$n" -ge 2 ]] || { case_fail "未形成矛盾双存（degraded=${n}）"; }

  # 3) 装配触发澄清：响应带问题
  http GET "/memory/bb/$uid"
  expect_status 200 || return 1
  expect_json_true '.clarification_needed | length > 0' || return 1

  # 4) 用户以 conversation 回答（澄清只消费 dialogue 事件）
  http POST /events "$(bb_conv_event "$uid" "$e3" '[{"role":"user","text":"以后都按详细来"},{"role":"assistant","text":"好的，以后回答尽量详细。"}]')"
  expect_status 202 || return 1

  # 5) 等 pending 被消费
  while ((waited < 120)); do
    n=$(db "SELECT count(*) FROM pending_clarifications WHERE user_source='bb' AND user_id='$uid'")
    [[ "$n" == "0" ]] && break
    sleep 2
    waited=$((waited + 2))
  done
  [[ "$n" == "0" ]] || { case_fail "回答后 pending 未消费（120s）"; }

  # 6) 收敛：规范行生效、conflict 变体归档、互标清除、装配不再提问
  expect_db "SELECT status FROM entries WHERE user_source='bb' AND user_id='$uid' AND topic='verbosity' AND variant='normal'" "active" || return 1
  expect_db "SELECT status FROM entries WHERE user_source='bb' AND user_id='$uid' AND topic='verbosity' AND variant='conflict'" "archived" || return 1
  expect_db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='$uid' AND topic='verbosity' AND variant IN ('normal','conflict') AND conflict_with IS NOT NULL" "0" || return 1
  http GET "/memory/bb/$uid"
  expect_status 200 || return 1
  expect_json '.clarification_needed | length' '0' || return 1
  case_pass
}

run_domain_lifecycle() {
  run_case T14 "矛盾收敛" --retry -- t14
  run_case T15 "decision流转" --retry -- t15
  run_case T16 "复核队列结构" -- t16
  run_case T17 "长消息触发" --retry -- t17
  run_case T18 "澄清回流闭环" --retry -- t18_clarify
}
