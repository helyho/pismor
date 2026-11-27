<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { Refresh, Search } from '@element-plus/icons-vue'

import { api } from '@/api/client'
import type { EntryConflictRow, EntryCreateResult, EntryFacets, EntryList, EntryListItem } from '@/api/types'
import { useAuthStore } from '@/auth'
import EntryCreateDialog from '@/components/EntryCreateDialog.vue'
import EntryDetailDrawer from '@/components/EntryDetailDrawer.vue'
import EntryEditDialog from '@/components/EntryEditDialog.vue'
import { formatDateTime } from '@/utils/format'

const auth = useAuthStore()
const queryClient = useQueryClient()
const router = useRouter()

const page = ref(1)
const pageSize = ref(50)
const qInput = ref('')
const q = ref('')
const dimension = ref('')
const status = ref('')
const source = ref('')
const userId = ref('')
const topic = ref('')
const order = ref<'updated_desc' | 'updated_asc' | 'confidence_desc'>('updated_desc')

const facetsQuery = useQuery<EntryFacets>({
  queryKey: ['entry-facets'],
  queryFn: () => api<EntryFacets>('/entries/facets'),
})

const entriesQuery = useQuery<EntryList>({
  queryKey: computed(() => [
    'entries',
    page.value,
    pageSize.value,
    q.value,
    dimension.value,
    status.value,
    source.value,
    userId.value,
    topic.value,
    order.value,
  ]),
  queryFn: () => {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
      order: order.value,
    })
    if (q.value) params.set('q', q.value)
    if (dimension.value) params.set('dimension', dimension.value)
    if (status.value) params.set('status', status.value)
    if (source.value) params.set('user_source', source.value)
    if (userId.value) params.set('user_id', userId.value)
    if (topic.value) params.set('topic', topic.value)
    return api<EntryList>(`/entries?${params.toString()}`)
  },
})

const items = computed(() => entriesQuery.data.value?.items ?? [])
const total = computed(() => entriesQuery.data.value?.total ?? 0)
const facets = computed(() => facetsQuery.data.value)

const statusLabels: Record<string, string> = {
  pending_review: '待复核',
  orphan_topic: '观察区',
  active: '启用',
  degraded: '降级',
  archived: '归档',
}

function statusLabel(value: string): string {
  return statusLabels[value] ?? value
}

function search(): void {
  q.value = qInput.value.trim()
  page.value = 1
}

function reset(): void {
  qInput.value = ''
  q.value = ''
  dimension.value = ''
  status.value = ''
  source.value = ''
  userId.value = ''
  topic.value = ''
  order.value = 'updated_desc'
  page.value = 1
}

const drawerVisible = ref(false)
const drawerTarget = ref<{
  user_source: string
  user_id: string
  entry_id: string
} | null>(null)

function openEntry(row: unknown): void {
  const item = row as EntryListItem
  drawerTarget.value = {
    user_source: item.user_source,
    user_id: item.user_id,
    entry_id: item.id,
  }
  drawerVisible.value = true
}

function openUser(row: unknown): void {
  const item = row as EntryListItem
  router.push({
    name: 'user-detail',
    params: { source: item.user_source, user: item.user_id },
  })
}

function filterBy(field: 'dimension' | 'topic' | 'source', value: string): void {
  if (field === 'dimension') dimension.value = value
  if (field === 'topic') topic.value = value
  if (field === 'source') source.value = value
  page.value = 1
}

const editVisible = ref(false)
const editTarget = ref<{
  user_source: string
  user_id: string
  entry_id: string
} | null>(null)

function openEdit(row: unknown): void {
  const item = row as EntryListItem
  editTarget.value = {
    user_source: item.user_source,
    user_id: item.user_id,
    entry_id: item.id,
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
  await Promise.all([entriesQuery.refetch(), facetsQuery.refetch()])
  void queryClient.invalidateQueries({ queryKey: ['overview'] })
}

const createVisible = ref(false)

// 新建成功：回到第一页（新条目按 updated_desc 排最前）并短暂高亮新行
const highlightId = ref<string | null>(null)
let highlightTimer: ReturnType<typeof setTimeout> | undefined

function rowClassName({ row }: { row: EntryListItem }): string {
  return row.id === highlightId.value ? 'created-row-highlight' : ''
}

function onCreateSaved(result: EntryCreateResult): void {
  page.value = 1
  highlightId.value = result.id ?? null
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => {
    highlightId.value = null
  }, 8000)
  void Promise.all([entriesQuery.refetch(), facetsQuery.refetch()])
  void queryClient.invalidateQueries({ queryKey: ['overview'] })
}

// 409 同键冲突“去编辑这条”：复用现有编辑抽屉（冲突行无用户上下文，取表单同键来源/用户）
function onCreateGotoEdit(
  existing: EntryConflictRow,
  target: { user_source: string; user_id: string },
): void {
  editTarget.value = { ...target, entry_id: existing.id }
  editVisible.value = true
}

onBeforeUnmount(() => {
  if (highlightTimer) clearTimeout(highlightTimer)
})
</script>

<template>
  <div class="page entries-page">
    <div class="page-head">
      <div>
        <h2>记忆</h2>
        <p class="page-subtitle">跨用户检索记忆，点击维度、主题或作用域可直接筛选。</p>
      </div>
      <div class="head-actions">
        <el-button
          v-if="auth.can('memory.write')"
          type="primary"
          @click="createVisible = true"
        >
          新建记忆
        </el-button>
        <el-button :icon="Refresh" :loading="entriesQuery.isLoading.value" @click="entriesQuery.refetch()">
          刷新
        </el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form label-position="top" class="filter-form" @submit.prevent>
        <div class="entry-filter-grid">
          <el-form-item label="来源">
            <el-select v-model="source" clearable placeholder="全部来源" @change="search">
              <el-option
                v-for="item in facets?.sources ?? []"
                :key="item.value"
                :label="`${item.value} (${item.count})`"
                :value="item.value"
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
                v-for="item in facets?.dimensions ?? []"
                :key="item.value"
                :label="`${item.value} (${item.count})`"
                :value="item.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="主题">
            <el-input
              v-model="topic"
              clearable
              placeholder="包含"
              @keyup.enter="search"
              @clear="search"
            />
          </el-form-item>
          <el-form-item label="状态">
            <el-select v-model="status" clearable placeholder="全部状态" @change="search">
              <el-option
                v-for="item in facets?.statuses ?? []"
                :key="item.value"
                :label="statusLabel(item.value)"
                :value="item.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="关键词">
            <el-input
              v-model="qInput"
              clearable
              placeholder="摘要 / 正文 / 主题 / 用户"
              @keyup.enter="search"
              @clear="search"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item label="排序">
            <el-select v-model="order" @change="search">
              <el-option label="最近更新" value="updated_desc" />
              <el-option label="最早更新" value="updated_asc" />
              <el-option label="置信最高" value="confidence_desc" />
            </el-select>
          </el-form-item>
        </div>
        <div class="entry-filter-actions">
          <span class="muted">共 {{ total }} 条记忆</span>
          <div class="entry-filter-buttons">
            <el-button type="primary" :icon="Search" @click="search">查询</el-button>
            <el-button @click="reset">重置</el-button>
          </div>
        </div>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-table
        v-loading="entriesQuery.isLoading.value"
        :data="items"
        size="small"
        :row-class-name="rowClassName"
        @row-click="openEntry"
      >
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" class="source-tag filter-link" @click.stop="filterBy('source', row.user_source)">
              {{ row.user_source }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="用户 ID" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button link type="primary" class="cell-link user-id" @click.stop="openUser(row)">
              {{ row.user_id }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="维度" width="130">
          <template #default="{ row }">
            <el-tag
              size="small"
              class="filter-link"
              @click.stop="filterBy('dimension', row.dimension)"
            >
              {{ row.dimension }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="主题" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button link type="primary" class="cell-link" @click.stop="filterBy('topic', row.topic)">
              {{ row.topic }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column prop="summary" label="摘要" min-width="260" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">{{ statusLabel(row.status) }}</template>
        </el-table-column>
        <el-table-column label="置信" width="80" align="right">
          <template #default="{ row }">{{ row.confidence.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column prop="sources_count" label="证据" width="70" align="right" />
        <el-table-column label="更新时间" min-width="180">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="" width="140">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openEntry(row)">查看</el-button>
            <el-button
              v-if="auth.can('memory.write')"
              link
              type="primary"
              @click.stop="openEdit(row)"
            >
              编辑
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!entriesQuery.isLoading.value && !items.length" description="没有匹配记忆" />
      <el-pagination
        v-if="total > pageSize"
        class="pager"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="pageSize"
        :current-page="page"
        background
        @current-change="(value: number) => (page = value)"
      />
    </el-card>

    <EntryDetailDrawer
      v-model="drawerVisible"
      :target="drawerTarget"
      :editable="auth.can('memory.write')"
      @edit="editCurrent"
    />

    <EntryEditDialog
      v-model="editVisible"
      :target="editTarget"
      :facets="facets"
      @saved="onEditSaved"
    />

    <EntryCreateDialog
      v-model="createVisible"
      @saved="onCreateSaved"
      @goto-edit="onCreateGotoEdit"
    />
  </div>
</template>

<style scoped>
.entries-page {
  max-width: none;
}

.filter-card {
  margin-bottom: 16px;
}

.filter-form :deep(.el-form-item) {
  margin-bottom: 8px;
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}

.cell-link {
  padding: 0;
  font-weight: 400;
}

.filter-link {
  cursor: pointer;
}

:deep(.el-table__row) {
  cursor: pointer;
}

:deep(.el-table .created-row-highlight td.el-table__cell) {
  background: var(--el-color-primary-light-9);
}
</style>
