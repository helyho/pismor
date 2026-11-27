<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { Delete, Refresh } from '@element-plus/icons-vue'

import { api, ApiError } from '@/api/client'
import type { AssembleResult, UserDetail, UserEntry } from '@/api/types'
import { useAuthStore } from '@/auth'
import EntryDetailDrawer from '@/components/EntryDetailDrawer.vue'
import EntryEditDialog from '@/components/EntryEditDialog.vue'
import { formatDateTime } from '@/utils/format'

const route = useRoute()
const queryClient = useQueryClient()
const auth = useAuthStore()

const source = computed(() => String(route.params.source ?? ''))
const userId = computed(() => String(route.params.user ?? ''))

const detailQuery = useQuery<UserDetail>({
  queryKey: computed(() => ['user-detail-page', source.value, userId.value]),
  queryFn: () =>
    api<UserDetail>(
      `/users/${encodeURIComponent(source.value)}/${encodeURIComponent(userId.value)}`,
    ),
})

const detail = computed(() => detailQuery.data.value)

const statusLabels: Record<string, string> = {
  pending_review: '待复核',
  orphan_topic: '观察区',
  active: '启用',
  degraded: '降级',
  archived: '归档',
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status
}

const groupedEntries = computed(() => {
  const groups = new Map<string, UserEntry[]>()
  for (const entry of detail.value?.entries ?? []) {
    const list = groups.get(entry.dimension) ?? []
    list.push(entry)
    groups.set(entry.dimension, list)
  }
  const order = ['taboo', 'identity', 'preference', 'fact', 'context']
  return [...groups.entries()].sort(
    (a, b) =>
      (order.indexOf(a[0]) === -1 ? 99 : order.indexOf(a[0])) -
      (order.indexOf(b[0]) === -1 ? 99 : order.indexOf(b[0])),
  )
})

const drawerVisible = ref(false)
const drawerTarget = ref<{
  user_source: string
  user_id: string
  entry_id: string
} | null>(null)

function openEntry(row: unknown): void {
  if (!detail.value) return
  const entry = row as UserEntry
  drawerTarget.value = {
    user_source: detail.value.user_source,
    user_id: detail.value.user_id,
    entry_id: entry.id,
  }
  drawerVisible.value = true
}

const editVisible = ref(false)
const editTarget = ref<{
  user_source: string
  user_id: string
  entry_id: string
} | null>(null)

function openEdit(row: unknown): void {
  if (!detail.value) return
  const entry = row as UserEntry
  editTarget.value = {
    user_source: detail.value.user_source,
    user_id: detail.value.user_id,
    entry_id: entry.id,
  }
  editVisible.value = true
}

function editCurrent(): void {
  if (!drawerTarget.value) return
  editTarget.value = drawerTarget.value
  editVisible.value = true
  drawerVisible.value = false
}

async function onEditSaved(): Promise<void> {
  drawerVisible.value = false
  await detailQuery.refetch()
  queryClient.invalidateQueries({ queryKey: ['users'] })
  queryClient.invalidateQueries({ queryKey: ['overview'] })
}

const budget = ref(2000)
const dimensions = ref<string[]>([])
const includeMemory = ref(true)
const queryText = ref('')
const assembleResult = ref<AssembleResult | null>(null)

const dimensionOptions = computed(() => detail.value?.by_dimension ?? [])

const assembleMutation = useMutation<AssembleResult, Error, void>({
  mutationFn: () => {
    const params = new URLSearchParams({
      budget: String(budget.value),
      include_memory: String(includeMemory.value),
    })
    if (dimensions.value.length) params.set('dimensions', dimensions.value.join(','))
    if (queryText.value.trim()) params.set('query', queryText.value.trim())
    return api<AssembleResult>(
      `/users/${encodeURIComponent(source.value)}/${encodeURIComponent(userId.value)}/assemble?${params.toString()}`,
    )
  },
  onSuccess: (data) => {
    assembleResult.value = data
  },
  onError: (error) => {
    ElMessage.error(error instanceof ApiError ? error.message : String(error))
  },
})

const deleting = ref(false)
const restoring = ref(false)
const purging = ref(false)

async function deleteUser(): Promise<void> {
  const current = detail.value
  if (!current) return
  try {
    await ElMessageBox.confirm(
      `确认删除用户「${current.user_source} / ${current.user_id}」？`
      + '这是软删除，记忆会保留并可在删除记录中恢复；彻底删除需在删除记录中操作。',
      '删除用户记忆',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  deleting.value = true
  try {
    await api<{ deleted: boolean }>(
      `/users/${encodeURIComponent(current.user_source)}/${encodeURIComponent(current.user_id)}`,
      { method: 'DELETE' },
    )
    ElMessage.success('用户已软删除，可在删除记录中恢复')
    queryClient.invalidateQueries({ queryKey: ['users'] })
    queryClient.invalidateQueries({ queryKey: ['overview'] })
    await detailQuery.refetch()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : String(error))
  } finally {
    deleting.value = false
  }
}

async function restoreUser(): Promise<void> {
  const current = detail.value
  if (!current?.tombstone || current.tombstone.purged_at) return
  try {
    await ElMessageBox.confirm(
      `恢复用户「${current.user_source} / ${current.user_id}」？记忆会重新进入列表与装配。`,
      '恢复用户',
      { type: 'info', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  restoring.value = true
  try {
    await api(
      `/users/${encodeURIComponent(current.user_source)}/${encodeURIComponent(current.user_id)}/restore`,
      { method: 'POST' },
    )
    ElMessage.success('用户已恢复')
    queryClient.invalidateQueries({ queryKey: ['users'] })
    queryClient.invalidateQueries({ queryKey: ['overview'] })
    await detailQuery.refetch()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : String(error))
  } finally {
    restoring.value = false
  }
}

async function purgeUser(): Promise<void> {
  const current = detail.value
  if (!current) return
  try {
    await ElMessageBox.confirm(
      `彻底删除用户「${current.user_source} / ${current.user_id}」？`
      + '记忆、证据链、处理历史与向量都会被清除，且不可恢复。',
      '彻底删除用户',
      {
        type: 'error',
        confirmButtonText: '彻底删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  purging.value = true
  try {
    await api(
      `/users/${encodeURIComponent(current.user_source)}/${encodeURIComponent(current.user_id)}/permanent`,
      { method: 'DELETE' },
    )
    ElMessage.success('用户数据已彻底删除')
    queryClient.invalidateQueries({ queryKey: ['users'] })
    queryClient.invalidateQueries({ queryKey: ['overview'] })
    await detailQuery.refetch()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : String(error))
  } finally {
    purging.value = false
  }
}
</script>

<template>
  <div class="page user-detail-page">
    <div class="page-head">
      <div class="head-left">
        <div>
          <h2>用户记忆</h2>
          <div class="identity-row">
            <span class="identity-item">
              <span class="identity-label">来源</span>
              <el-tag size="small" effect="plain" class="source-tag">{{ source }}</el-tag>
            </span>
            <span class="identity-item">
              <span class="identity-label">用户 ID</span>
              <span class="mono">{{ userId }}</span>
            </span>
          </div>
        </div>
      </div>
      <div class="head-actions">
        <el-button :icon="Refresh" :loading="detailQuery.isLoading.value" @click="detailQuery.refetch()">
          刷新
        </el-button>
        <el-button
          v-if="detail && !detail.tombstone"
          type="danger"
          :icon="Delete"
          :loading="deleting"
          @click="deleteUser"
        >
          删除用户
        </el-button>
        <el-button
          v-if="detail?.tombstone && !detail.tombstone.purged_at && auth.can('memory.write')"
          type="success"
          :loading="restoring"
          @click="restoreUser"
        >
          恢复用户
        </el-button>
        <el-button
          v-if="detail?.tombstone && !detail.tombstone.purged_at && auth.can('memory.delete')"
          type="danger"
          :icon="Delete"
          :loading="purging"
          @click="purgeUser"
        >
          彻底删除
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="detailQuery.isError.value"
      type="error"
      show-icon
      :closable="false"
      :title="`加载用户失败：${String(detailQuery.error.value)}`"
    />
    <el-skeleton v-else-if="detailQuery.isLoading.value" :rows="8" animated />

    <template v-else-if="detail">
      <el-alert
        v-if="detail.tombstone"
        class="tombstone-alert"
        :type="detail.tombstone.purged_at ? 'error' : 'warning'"
        show-icon
        :closable="false"
        :title="detail.tombstone.purged_at
          ? `该用户已彻底删除：${formatDateTime(detail.tombstone.purged_at)}`
          : `该用户已软删除，可恢复：${formatDateTime(detail.tombstone.deleted_at)}`"
        :description="detail.tombstone.purged_at
          ? `操作者：${detail.tombstone.requested_by}，数据不可恢复`
          : `操作者：${detail.tombstone.requested_by}，记忆保留，恢复后重新进入列表与装配`"
      />

      <el-row :gutter="16" class="stat-grid">
        <el-col :xs="12" :sm="6">
          <el-card shadow="never" class="stat-card">
            <div class="stat-label">记忆</div>
            <div class="stat-value">{{ detail.entries_count }}</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card shadow="never" class="stat-card">
            <div class="stat-label">待复核</div>
            <div class="stat-value">{{ detail.pending_review }}</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card shadow="never" class="stat-card">
            <div class="stat-label">冲突</div>
            <div class="stat-value">{{ detail.conflicts }}</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card shadow="never" class="stat-card">
            <div class="stat-label">最近更新</div>
            <div class="stat-time">{{ formatDateTime(detail.updated_at) }}</div>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" class="section-card" header="记忆">
        <el-empty v-if="!groupedEntries.length" description="没有记忆" />
        <section v-for="[dimension, entries] in groupedEntries" :key="dimension" class="dim-group">
          <h3>
            {{ dimension }}
            <el-tag size="small" type="info">{{ entries.length }}</el-tag>
          </h3>
          <el-table :data="entries" size="small" border>
            <el-table-column prop="topic" label="主题" min-width="120" />
                <el-table-column prop="summary" label="摘要" min-width="240" show-overflow-tooltip />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">{{ statusLabel(row.status) }}</template>
            </el-table-column>
            <el-table-column label="置信" width="80" align="right">
              <template #default="{ row }">{{ row.confidence.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column prop="reinforcements" label="强化" width="70" align="right" />
            <el-table-column prop="sources_count" label="证据" width="70" align="right" />
            <el-table-column label="更新时间" min-width="180">
              <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="" width="130">
              <template #default="{ row }">
                <el-button link type="primary" @click="openEntry(row)">查看</el-button>
                <el-button
                  v-if="auth.can('memory.write') && !detail.tombstone"
                  link
                  type="primary"
                  @click="openEdit(row)"
                >
                  编辑
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-card>

      <el-card shadow="never" class="section-card" header="装配预览">
        <el-form :inline="true" class="assemble-form" @submit.prevent>
          <el-form-item label="预算">
            <el-input-number v-model="budget" :min="1" :max="100000" :step="100" />
          </el-form-item>
          <el-form-item label="维度">
            <el-select
              v-model="dimensions"
              multiple
              clearable
              collapse-tags
              placeholder="全部相关维度"
              style="width: 176px"
            >
              <el-option
                v-for="item in dimensionOptions"
                :key="item.dimension"
                :label="item.dimension"
                :value="item.dimension"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="注入记忆">
            <el-switch v-model="includeMemory" />
          </el-form-item>
          <el-form-item label="话题">
            <el-input
              v-model="queryText"
              clearable
              placeholder="用于相关集召回，可留空"
              style="width: 220px"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="assembleMutation.isPending.value"
              @click="assembleMutation.mutate()"
            >
              生成预览
            </el-button>
          </el-form-item>
        </el-form>

        <template v-if="assembleResult">
          <div class="assemble-meta">
            <el-tag size="small">预算用量 {{ assembleResult.budget_used }}</el-tag>
            <el-tag v-if="assembleResult.merge_task" size="small" type="warning">触发合并任务</el-tag>
            <el-tag v-if="assembleResult.entries.length" size="small" type="success">
              {{ assembleResult.entries.length }} 条注入
            </el-tag>
          </div>
          <el-alert
            v-for="item in assembleResult.clarification_needed"
            :key="item.question"
            type="info"
            :closable="false"
            :title="item.question"
            :description="`涉及：${item.conflicting_ids.join('、')}`"
          />
          <el-table :data="assembleResult.entries" size="small" border empty-text="没有记忆进入预算">
            <el-table-column prop="id" label="记忆" min-width="180" />
            <el-table-column prop="text" label="注入文本" min-width="300" show-overflow-tooltip />
            <el-table-column label="置信" width="80" align="right">
              <template #default="{ row }">{{ row.confidence.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">{{ statusLabel(row.status) }}</template>
            </el-table-column>
          </el-table>
          <section v-if="assembleResult.rendered" class="render-preview">
            <h4>注入文本</h4>
            <pre>{{ assembleResult.rendered }}</pre>
          </section>
        </template>
      </el-card>

      <el-card
        v-if="detail.clarifications.length"
        shadow="never"
        class="section-card"
        header="待确认问题"
      >
        <el-alert
          v-for="item in detail.clarifications"
          :key="`${item.issued_at}-${item.question}`"
          type="info"
          :closable="false"
          :title="item.question"
          :description="`涉及：${item.conflicting_ids.join('、')}；有效期 ${item.ttl_days} 天`"
        />
      </el-card>
    </template>

    <EntryDetailDrawer
      v-model="drawerVisible"
      :target="drawerTarget"
      :editable="auth.can('memory.write') && !detail?.tombstone"
      @edit="editCurrent"
    />
    <EntryEditDialog v-model="editVisible" :target="editTarget" @saved="onEditSaved" />
  </div>
</template>

<style scoped>
.user-detail-page {
  max-width: none;
}

.head-left,
.head-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.head-left h2 {
  margin: 0;
}

.identity-row {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 7px;
  flex-wrap: wrap;
}

.identity-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.identity-label {
  color: var(--app-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
}

.tombstone-alert {
  margin-bottom: 16px;
}

.stat-card {
  height: 100%;
}

.stat-time {
  display: flex;
  align-items: center;
  min-height: 34px;
  margin-top: 8px;
  font-size: 14px;
  color: #6b7280;
  white-space: nowrap;
}

.section-card {
  margin-bottom: 16px;
}

.dim-group + .dim-group {
  margin-top: 18px;
}

.dim-group h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 10px;
  font-size: 15px;
}

.assemble-form :deep(.el-form-item) {
  margin-bottom: 10px;
}

.assemble-meta {
  display: flex;
  gap: 8px;
  margin: 4px 0 12px;
}

.render-preview {
  margin-top: 16px;
}

.render-preview h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

.render-preview pre {
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
