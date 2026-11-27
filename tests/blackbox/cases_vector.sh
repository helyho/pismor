#!/usr/bin/env bash
# 域7 向量召回（T26）：真实 embedding 端点，判别"向量优先"分支确实被走到。
#
# 判别法：query 与条目正文的 jieba token 交集为空 ⇒ 词面召回（BM25）必为空；
#         此时仍能召回 ⇒ 命中只可能来自向量分支。另断言 embeddings 表存有该条向量行，
#         排除"向量行没写却命中"的假阳性（vec0 虚拟表必须走 pismor 连接查询）。
# 前置条件（候选 query 的词面/相似度）不满足时 SKIP 而非 FAIL：
# 抽取正文由 LLM 改写，属输入随机性，不是产品缺陷。
t26() {
  need_llm || return 2
  need_embed || return 2
  local uid="vecu-$$-$RANDOM" ev body q cos n hit i
  ev=$(next_event_id veca)
  http POST /events "$(bb_event "$uid" "$ev" "记住, 我日常写脚本都用 Python")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM sources WHERE user_source='bb' AND user_id='${uid}' AND origin_ref='blackbox:${ev}'" 120 \
    || { case_fail "120s 内事件未落库"; }
  for i in 1 2 3; do
    activate_all "$uid" || { case_fail "activate_all 失败"; }
    wait_db "SELECT 1 FROM entries WHERE user_source='bb' AND user_id='${uid}' AND status='active'" 15 && break
  done
  body=$(db "SELECT body FROM entries WHERE user_source='bb' AND user_id='${uid}' AND status='active' ORDER BY updated_at DESC, rowid DESC LIMIT 1")
  [[ -n "$body" ]] || { case_fail "未找到 active 条目"; }
  n=$(vec_count "$uid")
  [[ "${n:-0}" -ge 1 ]] || { case_fail "embeddings 无该用户向量行（count=$n）"; }
  IFS=$'\t' read -r q cos <<<"$(pick_semantic_query "$body")"
  [[ -n "$q" ]] || { printf '未找到与正文词面无交集、cosine 有余量的 query（body=%s）\n' "$body"; return 2; }
  http GET "/memory/bb/${uid}?query=$(urienc "$q")"
  expect_status 200 || return 1
  hit=$(printf '%s' "$BODY" | jq --arg b "$body" \
    '[.entries[] | select(.text == $b or .text == ("曾观察：" + $b))] | length')
  if [[ "${hit:-0}" -lt 1 ]]; then
    case_fail "语义 query [$q](cos=$cos, 与正文词面无交集) 未召回条目；实际=$(printf '%s' "$BODY" | jq -c '[.entries[].text]')"
  fi
  case_pass
}

run_domain_vector() {
  run_case T26 "向量召回生效（词面无交集判别）" -- t26
}
