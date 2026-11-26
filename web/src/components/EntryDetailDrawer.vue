<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'

import { api } from '@/api/client'
import type { ReviewDetail } from '@/api/types'
import { formatDateTime } from '@/utils/format'

const router = useRouter()

interface Target {
  user_source: string
  user_id: string
  entry_id: string
  scope: string
}

const props = defineProps<{
  modelValue: boolean
  target: Target | null
  editable?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  edit: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const query = useQuery<ReviewDetail>({
  queryKey: computed(() => [
    'entry-detail',
    props.target?.user_source,
    props.target?.user_id,
    props.target?.entry_id,
    props.target?.scope,
  ]),
  queryFn: () => {
    if (!props.target) throw new Error('no target')
    const params = new URLSearchParams({
      user_source: props.target.user_source,
      user_id: props.target.user_id,
      entry_id: props.target.entry_id,
      scope: props.target.scope,
    })
    return api<ReviewDetail>(`/review/entry?${params.toString()}`)
  },
  enabled: computed(() => props.modelValue && props.target !== null),
})

const detail = computed(() => query.data.value)

const statusLabels: Record<string, string> = {
  pending_review: '待复核',
  orphan_topic: '观察区',
  active: '启用',
  degraded: '降级',
  archived: '归档',
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

function goUser(): void {
  if (!detail.value) return
  router.push({
    name: 'user-detail',
    params: { source: detail.value.entry.user_source, user: detail.value.entry.user_id },
  })
  visible.value = false
}
</script>

<template>
  <el-drawer v-model="visible" title="记忆详情" size="760px">
    <el-skeleton v-if="query.isLoading.value" :rows="10" animated />
    <el-alert
      v-else-if="query.isError.value"
      type="error"
      show-icon
      :closable="false"
      :title="`加载记忆失败：${String(query.error.value)}`"
    />
    <template v-else-if="detail">
      <div class="drawer-summary">{{ detail.entry.summary }}</div>

      <el-alert
        v-if="detail.entry.conflict_with"
        class="drawer-alert"
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

      <el-descriptions :column="2" border size="small" class="drawer-meta">
        <el-descriptions-item label="来源">{{ detail.entry.user_source }}</el-descriptions-item>
        <el-descriptions-item label="用户 ID">
          <el-button link type="primary" class="user-link user-id" @click="goUser">
            {{ detail.entry.user_id }}
          </el-button>
        </el-descriptions-item>
        <el-descriptions-item label="维度">{{ detail.entry.dimension }}</el-descriptions-item>
        <el-descriptions-item label="主题">{{ detail.entry.topic }}</el-descriptions-item>
        <el-descriptions-item label="作用域">{{ detail.entry.scope }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ statusLabel(detail.entry.status) }}</el-descriptions-item>
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

      <section class="drawer-section">
        <h4>内容</h4>
        <pre class="body-text">{{ detail.entry.body }}</pre>
      </section>

      <section class="drawer-section">
        <h4>证据链 <span class="muted">{{ detail.sources.length }}</span></h4>
        <el-table :data="detail.sources" size="small" border empty-text="暂无证据">
          <el-table-column prop="kind" label="类型" width="90" />
          <el-table-column prop="origin_type" label="来源" width="100" />
          <el-table-column prop="origin_ref" label="引用" min-width="150" show-overflow-tooltip />
          <el-table-column prop="quote" label="原文" min-width="220" show-overflow-tooltip />
        </el-table>
      </section>

      <section class="drawer-section">
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

      <section v-if="detail.clarifications.length" class="drawer-section">
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

      <div v-if="editable" class="drawer-actions">
        <el-button type="primary" @click="emit('edit')">编辑记忆</el-button>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.user-link {
  padding: 0;
  font-weight: 400;
}

.drawer-summary {
  font-size: 18px;
  font-weight: 600;
  line-height: 1.5;
  margin-bottom: 12px;
}

.drawer-alert {
  margin-bottom: 12px;
}

.conflict-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}

.drawer-meta {
  margin-bottom: 16px;
}

.drawer-section {
  margin-top: 20px;
}

.drawer-actions {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.drawer-section h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

.body-text {
  margin: 0;
  padding: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  background: #f7f8fa;
  border-radius: 6px;
  font-family: inherit;
  line-height: 1.6;
}
</style>
