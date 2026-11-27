<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { Check, Delete, Edit, Refresh, Search } from '@element-plus/icons-vue'

import { api, ApiError } from '@/api/client'
import type {
  ReviewDecision,
  ReviewDecisionPayload,
  ReviewDecisionResult,
  ReviewDetail,
  ReviewFacets,
  ReviewQueue,
  ReviewQueueItem,
} from '@/api/types'
import { formatDateTime } from '@/utils/format'

const queryClient = useQueryClient()
const router = useRouter()

const page = ref(1)
const pageSize = ref(50)
const dimension = ref('')
const source = ref('')
const userId = ref('')
const qInput = ref('')
const q = ref('')
const order = ref<'oldest' | 'newest'>('oldest')
const selectedKey = ref<string | null>(null)
const pendingIndex = ref(0)
const note = ref('')
const targetTopic = ref('')
const fixVisible = ref(false)
const fixSummary = ref('')
const fixBody = ref('')

const queueQuery = useQuery<ReviewQueue>({
  queryKey: computed(() => [
    'review-queue',
    page.value,
    pageSize.value,
    dimension.value,
    source.value,
    userId.value,
    q.value,
    order.value,
  ]),
  queryFn: () => {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
      order: order.value,
    })
    if (dimension.value) params.set('dimension', dimension.value)
    if (source.value) params.set('user_source', source.value)
    if (userId.value) params.set('user_id', userId.value)
    if (q.value) params.set('q', q.value)
    return api<ReviewQueue>(`/review/queue?${params.toString()}`)
  },
})

const {
  data: queueData,
  isLoading: queueLoading,
  isError: queueIsError,
  error: queueError,
  refetch: refetchQueue,
} = queueQuery

const facetsQuery = useQuery<ReviewFacets>({
  queryKey: ['review-facets'],
  queryFn: () => api<ReviewFacets>('/review/facets'),
})

const items = computed(() => queueData.value?.items ?? [])
const total = computed(() => queueData.value?.total ?? 0)
const dimensionOptions = computed(() => facetsQuery.data.value?.dimensions ?? [])
const sourceOptions = computed(() => facetsQuery.data.value?.sources ?? [])

function keyOf(item: ReviewQueueItem): string {
  return [item.user_source, item.user_id, item.entry_id].join('\u0000')
}

const selected = computed(
  () => items.value.find((item) => keyOf(item) === selectedKey.value) ?? null,
)

watch(
  items,
  (list) => {
    if (!list.length) {
      selectedKey.value = null
      return
    }
    if (!list.some((item) => keyOf(item) === selectedKey.value)) {
      selectedKey.value = keyOf(list[0])
    }
  },
  { immediate: true },
)

const detailQuery = useQuery<ReviewDetail>({
  queryKey: computed(() => ['review-detail', selectedKey.value]),
  queryFn: () => {
    const item = selected.value
    if (!item) throw new Error('no selection')
    const params = new URLSearchParams({
      user_source: item.user_source,
      user_id: item.user_id,
      entry_id: item.entry_id,
    })
    return api<ReviewDetail>(`/review/entry?${params.toString()}`)
  },
  enabled: computed(() => selected.value !== null),
})

const {
  data: detailData,
  isLoading: detailLoading,
  isError: detailIsError,
  error: detailError,
  refetch: refetchDetail,
} = detailQuery

const detail = computed(() => detailData.value)

watch(selectedKey, () => {
  note.value = ''
})

const statusLabels: Record<string, string> = {
  pending_review: '待复核',
  orphan_topic: '观察区',
  active: '启用',
  degraded: '降级',
  archived: '归档',
}

const decisionLabels: Record<ReviewDecision, string> = {
  confirm: '确认保留',
  remove: '移除',
  fix: '修正',
}

const historyLabels: Record<string, string> = {
  confirm: '确认保留',
  remove: '移除',
  fix: '修正',
  flagged: '标记',
  audit_pending: '审计待处理',
  expired: '过期',
  apply_failed: '应用失败',
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status
}

function historyLabel(decision: string): string {
  return historyLabels[decision] ?? decision
}

function openUser(): void {
  const entry = detailData.value?.entry
  if (!entry) return
  router.push({
    name: 'user-detail',
    params: { source: entry.user_source, user: entry.user_id },
  })
}

function search(): void {
  q.value = qInput.value.trim()
  page.value = 1
}

function resetFilters(): void {
  dimension.value = ''
  source.value = ''
  userId.value = ''
  qInput.value = ''
  q.value = ''
  order.value = 'oldest'
  page.value = 1
}

function onPageChange(value: number): void {
  page.value = value
}

watch(
  () => detailData.value?.entry.id,
  () => {
    targetTopic.value = detailData.value?.entry.topic ?? ''
  },
  { immediate: true },
)

async function refresh(): Promise<void> {
  await refetchQueue()
  if (selected.value) await refetchDetail()
}

function payloadFor(
  decision: ReviewDecision,
  extra: { body?: string; summary?: string; topic?: string } = {},
): ReviewDecisionPayload | null {
  const item = selected.value
  if (!item) return null
  return {
    user_source: item.user_source,
    user_id: item.user_id,
    entry_id: item.entry_id,
    decision,
    entry_version: detailData.value?.entry.updated_at ?? item.entry_version,
    note: note.value.trim() || undefined,
    ...extra,
  }
}

const decisionMutation = useMutation<ReviewDecisionResult, Error, ReviewDecisionPayload>({
  mutationFn: (payload) =>
    api<ReviewDecisionResult>('/review/decision', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  onSuccess: async (result, payload) => {
    if (result.noop) {
      ElMessage.warning('记忆已被其他人更新，本次判定作废，已刷新')
    } else if (result.invalid_topic) {
      ElMessage.warning('目标主题不在该维度白名单内')
    } else if (result.merged_into) {
      ElMessage.success(`已合并到 ${result.merged_into}`)
    } else if (result.rekeyed_to) {
      ElMessage.success(`已归一为 ${result.rekeyed_to}`)
    } else if (payload.decision === 'fix') {
      ElMessage.success('内容已修正，记忆仍需确认')
    } else {
      ElMessage.success(`${decisionLabels[payload.decision]}已生效`)
    }
    note.value = ''
    await Promise.all([refetchQueue(), refetchDetail()])
    if (!result.noop && payload.decision !== 'fix') {
      const list = items.value
      if (list.length) {
        selectedKey.value = keyOf(list[Math.min(pendingIndex.value, list.length - 1)])
      }
    }
    queryClient.invalidateQueries({ queryKey: ['overview'] })
  },
  onError: (error) => {
    ElMessage.error(error instanceof ApiError ? error.message : String(error))
  },
})

const deciding = computed(() => decisionMutation.isPending.value)

function mutateDecision(payload: ReviewDecisionPayload): void {
  pendingIndex.value = Math.max(
    items.value.findIndex((item) => keyOf(item) === selectedKey.value),
    0,
  )
  decisionMutation.mutate(payload)
}

function confirmSelected(): void {
  if (!selected.value) return
  const current = detailData.value?.entry.topic
  const extra = targetTopic.value && targetTopic.value !== current
    ? { topic: targetTopic.value }
    : {}
  const payload = payloadFor('confirm', extra)
  if (payload) mutateDecision(payload)
}

async function removeSelected(): Promise<void> {
  const item = selected.value
  if (!item) return
  try {
    await ElMessageBox.confirm(
      `确认移除「${item.summary}」？记忆与证据链会一起删除。`,
      '移除记忆',
      {
        type: 'warning',
        confirmButtonText: '移除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  const payload = payloadFor('remove')
  if (payload) mutateDecision(payload)
}

function openFix(): void {
  if (!detailData.value) return
  fixSummary.value = detailData.value.entry.summary
  fixBody.value = detailData.value.entry.body
  fixVisible.value = true
}

function submitFix(): void {
  if (!fixSummary.value.trim() && !fixBody.value.trim()) {
    ElMessage.warning('摘要和正文至少填写一项')
    return
  }
  const payload = payloadFor('fix', {
    summary: fixSummary.value.trim() || undefined,
    body: fixBody.value.trim() || undefined,
  })
  if (payload) {
    fixVisible.value = false
    mutateDecision(payload)
  }
}

function move(delta: number): void {
  const list = items.value
  if (!list.length) return
  const index = list.findIndex((item) => keyOf(item) === selectedKey.value)
  const next = Math.min(Math.max(index + delta, 0), list.length - 1)
  selectedKey.value = keyOf(list[next])
}

function isTyping(event: KeyboardEvent): boolean {
  const target = event.target as HTMLElement | null
  if (!target) return false
  return (
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.isContentEditable ||
    Boolean(target.closest('.el-select'))
  )
}

function onKeydown(event: KeyboardEvent): void {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return
  if (isTyping(event)) return
  switch (event.key.toLowerCase()) {
    case 'j':
      move(1)
      break
    case 'k':
      move(-1)
      break
    case 'a':
      confirmSelected()
      break
    case 'd':
      void removeSelected()
      break
    case 'f':
      openFix()
      break
    default:
      return
  }
  event.preventDefault()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="page review-page">
    <div class="page-head">
      <div>
        <h2>复核</h2>
        <p class="page-subtitle">按条处理待复核内容，处理完自动进入下一条。</p>
      </div>
      <div class="head-actions">
        <span class="muted">J/K 切换 · A 确认 · D 移除 · F 修正</span>
        <el-button :icon="Refresh" :loading="queueLoading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form label-position="top" class="filter-form" @submit.prevent>
        <div class="entry-filter-grid">
          <el-form-item label="来源">
            <el-select v-model="source" clearable placeholder="全部来源" @change="search">
              <el-option
                v-for="option in sourceOptions"
                :key="option.value"
                :label="`${option.value} (${option.count})`"
                :value="option.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="用户 ID">
            <el-input
              v-model="userId"
              clearable
              placeholder="精确匹配"
              @keyup.enter="search"
              @clear="search"
            />
          </el-form-item>
          <el-form-item label="维度">
            <el-select v-model="dimension" clearable placeholder="全部维度" @change="search">
              <el-option
                v-for="option in dimensionOptions"
                :key="option.value"
                :label="`${option.value} (${option.count})`"
                :value="option.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="关键词">
            <el-input
              v-model="qInput"
              clearable
              placeholder="摘要 / 正文 / 用户"
              @keyup.enter="search"
              @clear="search"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item label="排序">
            <el-select v-model="order" @change="search">
              <el-option label="最早优先" value="oldest" />
              <el-option label="最新优先" value="newest" />
            </el-select>
          </el-form-item>
        </div>
        <div class="entry-filter-actions">
          <span class="muted">共 {{ total }} 条待处理记忆</span>
          <div class="entry-filter-buttons">
            <el-button type="primary" :icon="Search" @click="search">查询</el-button>
            <el-button @click="resetFilters">重置</el-button>
          </div>
        </div>
      </el-form>
    </el-card>

    <div class="review-layout">
      <el-card shadow="never" class="queue-card">
        <div class="queue-head">
          <span>待处理</span>
          <el-tag size="small" type="info">{{ total }}</el-tag>
        </div>
        <el-skeleton v-if="queueLoading" :rows="8" animated />
        <el-alert
          v-else-if="queueIsError"
          type="error"
          show-icon
          :closable="false"
          :title="`加载队列失败：${String(queueError)}`"
        />
        <el-empty v-else-if="!items.length" description="没有待处理记忆" />
        <div v-else class="queue-list">
          <div
            v-for="item in items"
            :key="keyOf(item)"
            class="queue-item"
            :class="{ active: keyOf(item) === selectedKey }"
            @click="selectedKey = keyOf(item)"
          >
            <div class="queue-item-head">
              <el-tag size="small">{{ item.dimension }}</el-tag>
              <el-tag size="small" type="info">{{ item.variant }}</el-tag>
            </div>
            <div class="user-cell queue-user-cell">
              <span class="source">{{ item.user_source }}</span>
              <span class="uid">{{ item.user_id }}</span>
            </div>
            <div class="queue-summary">{{ item.summary }}</div>
            <div class="queue-meta">
              <span>置信 {{ item.confidence.toFixed(2) }}</span>
              <span>{{ formatDateTime(item.entry_version) }}</span>
            </div>
            <div v-if="item.note" class="queue-note">{{ item.note }}</div>
          </div>
        </div>
        <el-pagination
          v-if="total > pageSize"
          class="queue-pager"
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          small
          background
          @current-change="onPageChange"
        />
      </el-card>

      <el-card shadow="never" class="detail-card">
        <template #header>
          <div class="detail-head">
            <span>记忆详情</span>
            <el-tag v-if="detail" size="small" :type="detail.entry.status === 'active' ? 'success' : 'warning'">
              {{ statusLabel(detail.entry.status) }}
            </el-tag>
          </div>
        </template>

        <el-empty v-if="!selected" description="从左侧选择一个记忆" />
        <el-skeleton v-else-if="detailLoading" :rows="10" animated />
        <el-alert
          v-else-if="detailIsError"
          type="error"
          show-icon
          :closable="false"
          :title="`加载记忆失败：${String(detailError)}`"
        />
        <template v-else-if="detail">
          <div class="detail-summary">{{ detail.entry.summary }}</div>

          <el-alert
            v-if="detail.entry.conflict_with"
            class="conflict-alert"
            :type="detail.entry.status === 'archived' ? 'info' : 'warning'"
            show-icon
            :closable="false"
            :title="
              detail.entry.status === 'archived'
                ? '历史冲突记录（记忆已归档，仅留痕）'
                : '存在未消解的冲突'
            "
          >
            <div v-for="conflict in detail.conflicts" :key="conflict.id" class="conflict-row">
              <el-tag size="small" type="warning">{{ statusLabel(conflict.status) }}</el-tag>
              <span>{{ conflict.summary }}</span>
              <span class="muted">{{ conflict.id }}</span>
            </div>
          </el-alert>

          <el-descriptions :column="2" border size="small" class="detail-meta">
            <el-descriptions-item label="来源">{{ detail.entry.user_source }}</el-descriptions-item>
            <el-descriptions-item label="用户 ID">
              <el-button link type="primary" class="user-link user-id" @click="openUser">
                {{ detail.entry.user_id }}
              </el-button>
            </el-descriptions-item>
            <el-descriptions-item label="维度">{{ detail.entry.dimension }}</el-descriptions-item>
            <el-descriptions-item label="变体">{{ detail.entry.variant }}</el-descriptions-item>
            <el-descriptions-item label="变体序号">{{ detail.entry.variant_seq }}</el-descriptions-item>
            <el-descriptions-item label="主题">
              <el-select
                v-if="detail.allowed_topics.length"
                v-model="targetTopic"
                size="small"
                filterable
                class="topic-select"
              >
                <el-option
                  v-for="t in detail.allowed_topics"
                  :key="t"
                  :label="t"
                  :value="t"
                />
                <el-option :label="detail.entry.topic" :value="detail.entry.topic" />
              </el-select>
              <span v-else>{{ detail.entry.topic }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="置信度">{{ detail.entry.confidence.toFixed(2) }}</el-descriptions-item>
            <el-descriptions-item label="强化次数">{{ detail.entry.reinforcements }}</el-descriptions-item>
            <el-descriptions-item label="最近强化">
              {{ formatDateTime(detail.entry.last_reinforced_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="更新时间" :span="2">
              {{ formatDateTime(detail.entry.updated_at) }}
            </el-descriptions-item>
            <el-descriptions-item v-if="detail.entry.degraded_reason" label="降级原因" :span="2">
              {{ detail.entry.degraded_reason }}
            </el-descriptions-item>
          </el-descriptions>

          <section class="detail-section">
            <h4>内容</h4>
            <pre class="body-text">{{ detail.entry.body }}</pre>
          </section>

          <section class="detail-section">
            <h4>证据链 <span class="muted">{{ detail.sources.length }}</span></h4>
            <el-table :data="detail.sources" size="small" border empty-text="暂无证据">
              <el-table-column prop="kind" label="类型" width="90" />
              <el-table-column prop="origin_type" label="来源" width="100" />
              <el-table-column prop="origin_ref" label="引用" min-width="150" show-overflow-tooltip />
              <el-table-column prop="quote" label="原文" min-width="220" show-overflow-tooltip />
            </el-table>
          </section>

          <section class="detail-section">
            <h4>处理历史</h4>
            <el-table :data="detail.history" size="small" border empty-text="暂无处理历史">
              <el-table-column label="处理结果" width="110">
                <template #default="{ row }">{{ historyLabel(row.decision) }}</template>
              </el-table-column>
              <el-table-column prop="actor" label="操作者" width="100" />
              <el-table-column label="时间" width="190">
                <template #default="{ row }">{{ formatDateTime(row.ts) }}</template>
              </el-table-column>
              <el-table-column prop="note" label="备注" min-width="160" show-overflow-tooltip />
            </el-table>
          </section>

          <section v-if="detail.clarifications.length" class="detail-section">
            <h4>待确认问题</h4>
            <el-alert
              v-for="clarification in detail.clarifications"
              :key="`${clarification.issued_at}-${clarification.question}`"
              type="info"
              :closable="false"
              :title="clarification.question"
              :description="`涉及：${clarification.conflicting_ids.join('、')}；有效期 ${clarification.ttl_days} 天`"
            />
          </section>

          <div class="review-actions">
            <el-input v-model="note" clearable placeholder="处理备注（可选）" />
            <el-button type="primary" :icon="Check" :loading="deciding" @click="confirmSelected">
              确认保留 <kbd>A</kbd>
            </el-button>
            <el-button type="warning" :icon="Edit" :disabled="!detail" @click="openFix">
              修正 <kbd>F</kbd>
            </el-button>
            <el-button type="danger" :icon="Delete" :loading="deciding" @click="removeSelected">
              移除 <kbd>D</kbd>
            </el-button>
          </div>
        </template>
      </el-card>
    </div>

    <el-dialog v-model="fixVisible" title="修正内容" width="620px">
      <el-form label-position="top">
        <el-form-item label="摘要">
          <el-input v-model="fixSummary" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input v-model="fixBody" type="textarea" :rows="8" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fixVisible = false">取消</el-button>
        <el-button type="primary" :loading="deciding" @click="submitFix">保存修正</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.review-page {
  max-width: none;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.filter-card {
  margin-bottom: 16px;
}

.filter-form :deep(.el-form-item) {
  margin-bottom: 0;
}

.review-layout {
  display: grid;
  grid-template-columns: 400px minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}

.queue-card {
  position: sticky;
  top: 18px;
}

.queue-head,
.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 650;
}

.queue-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: calc(100vh - 320px);
  overflow-y: auto;
  padding-right: 4px;
}

.queue-item {
  border: 1px solid transparent;
  border-radius: 11px;
  padding: 12px 13px;
  cursor: pointer;
  background: #f8fafc;
  transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
}

.queue-item:hover {
  background: #f1f5ff;
  border-color: #dfe5f5;
}

.queue-item.active {
  border-color: var(--app-primary);
  background: var(--app-primary-soft);
  box-shadow: 0 8px 20px -18px rgba(79, 70, 229, 0.8);
}

.queue-item-head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.queue-user-cell {
  margin-top: 8px;
}

.queue-summary {
  margin-top: 7px;
  font-size: 14px;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.queue-meta {
  display: flex;
  gap: 10px;
  margin-top: 7px;
  color: #94a3b8;
  font-size: 11px;
}

.queue-note {
  margin-top: 4px;
  color: #b45309;
  font-size: 12px;
}

.queue-pager {
  margin-top: 12px;
  justify-content: center;
}

.detail-card {
  min-height: 420px;
}

.detail-summary {
  font-size: 18px;
  font-weight: 600;
  line-height: 1.5;
  margin-bottom: 12px;
}

.conflict-alert {
  margin-bottom: 12px;
}

.conflict-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}

.user-link {
  padding: 0;
  font-weight: 400;
}

.detail-meta {
  margin-bottom: 16px;
}

.detail-section {
  margin-top: 20px;
}

.detail-section h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

.body-text {
  margin: 0;
  padding: 14px 16px;
  background: #f8fafc;
  border: 1px solid #eef2f7;
  border-radius: 10px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  line-height: 1.65;
}

.review-actions {
  position: sticky;
  bottom: -20px;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 22px -20px -20px;
  padding: 14px 20px;
  border-top: 1px solid var(--app-border);
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(8px);
}

.review-actions .el-input {
  max-width: 280px;
}

.review-actions kbd {
  margin-left: 4px;
  padding: 0 4px;
  border: 1px solid rgba(255, 255, 255, 0.5);
  border-radius: 3px;
  font-size: 11px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (max-width: 1100px) {
  .review-layout {
    grid-template-columns: 1fr;
  }

  .queue-card {
    position: static;
  }

  .queue-list {
    max-height: 360px;
  }

  .review-actions {
    flex-wrap: wrap;
  }
}
</style>
