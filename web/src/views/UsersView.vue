<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api } from '@/api/client'
import type { TombstoneItem, TombstoneList, UserList } from '@/api/types'
import { useAuthStore } from '@/auth'
import { formatDateTime } from '@/utils/format'

const router = useRouter()
const auth = useAuthStore()
const tab = ref<'users' | 'tombstones'>('users')

const page = ref(1)
const pageSize = ref(50)
const qInput = ref('')
const q = ref('')
const order = ref<'recent' | 'oldest' | 'entries'>('recent')

const tombstonePage = ref(1)
const tombstoneQ = ref('')

const usersQuery = useQuery<UserList>({
  queryKey: computed(() => ['users', page.value, pageSize.value, q.value, order.value]),
  queryFn: () => {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
      order: order.value,
    })
    if (q.value) params.set('q', q.value)
    return api<UserList>(`/users?${params.toString()}`)
  },
})

const tombstonesQuery = useQuery<TombstoneList>({
  queryKey: computed(() => [
    'tombstones',
    tombstonePage.value,
    pageSize.value,
    tombstoneQ.value,
  ]),
  queryFn: () => {
    const params = new URLSearchParams({
      page: String(tombstonePage.value),
      page_size: String(pageSize.value),
    })
    if (tombstoneQ.value) params.set('q', tombstoneQ.value)
    return api<TombstoneList>(`/users/tombstones?${params.toString()}`)
  },
  enabled: computed(() => tab.value === 'tombstones'),
})

const users = computed(() => usersQuery.data.value?.items ?? [])
const usersTotal = computed(() => usersQuery.data.value?.total ?? 0)
const tombstones = computed(() => tombstonesQuery.data.value?.items ?? [])
const tombstonesTotal = computed(() => tombstonesQuery.data.value?.total ?? 0)

function search(): void {
  q.value = qInput.value.trim()
  page.value = 1
}

function searchTombstones(): void {
  tombstoneQ.value = qInput.value.trim()
  tombstonePage.value = 1
}

function resetTombstones(): void {
  qInput.value = ''
  tombstoneQ.value = ''
  tombstonePage.value = 1
}

function reset(): void {
  qInput.value = ''
  q.value = ''
  page.value = 1
}

function openUser(row: unknown): void {
  const item = row as { user_source: string; user_id: string }
  router.push({
    name: 'user-detail',
    params: { source: item.user_source, user: item.user_id },
  })
}

function userPath(item: TombstoneItem, suffix = ''): string {
  return `/users/${encodeURIComponent(item.user_source)}/${encodeURIComponent(item.user_id)}${suffix}`
}

async function restoreTombstone(row: unknown): Promise<void> {
  const item = row as TombstoneItem
  try {
    await ElMessageBox.confirm(
      `恢复用户「${item.user_source} / ${item.user_id}」？恢复后记忆会重新进入列表与装配。`,
      '恢复用户',
      { type: 'info', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api(userPath(item, '/restore'), { method: 'POST' })
    ElMessage.success('用户已恢复')
    await Promise.all([usersQuery.refetch(), tombstonesQuery.refetch()])
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  }
}

async function purgeTombstone(row: unknown): Promise<void> {
  const item = row as TombstoneItem
  try {
    await ElMessageBox.confirm(
      `彻底删除用户「${item.user_source} / ${item.user_id}」？记忆、证据链、处理历史与向量都会被清除，且不可恢复。`,
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
  try {
    await api(userPath(item, '/permanent'), { method: 'DELETE' })
    ElMessage.success('用户数据已彻底删除')
    await Promise.all([usersQuery.refetch(), tombstonesQuery.refetch()])
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  }
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>用户</h2>
        <p class="page-subtitle">按来源与用户 ID 查找记忆，进入详情查看记忆与装配预览。</p>
      </div>
      <el-button :icon="Refresh" :loading="usersQuery.isLoading.value" @click="usersQuery.refetch()">
        刷新
      </el-button>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane label="用户列表" name="users">
        <el-card shadow="never" class="filter-card">
          <el-form label-position="top" class="filter-form" @submit.prevent>
            <div class="entry-filter-grid">
              <el-form-item label="关键词">
                <el-input
                  v-model="qInput"
                  clearable
                  placeholder="来源 / 用户 ID"
                  @keyup.enter="search"
                  @clear="reset"
                >
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>
              </el-form-item>
              <el-form-item label="排序">
                <el-select v-model="order" @change="search">
                  <el-option label="最近更新" value="recent" />
                  <el-option label="最早更新" value="oldest" />
                  <el-option label="记忆最多" value="entries" />
                </el-select>
              </el-form-item>
            </div>
            <div class="entry-filter-actions">
              <span class="muted">共 {{ usersTotal }} 个用户</span>
              <div class="entry-filter-buttons">
                <el-button type="primary" :icon="Search" @click="search">查询</el-button>
                <el-button @click="reset">重置</el-button>
              </div>
            </div>
          </el-form>
        </el-card>

        <el-card shadow="never">
          <el-table
            v-loading="usersQuery.isLoading.value"
            :data="users"
            size="small"
            row-key="user_id"
            @row-click="openUser"
          >
            <el-table-column label="来源" width="120">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" class="source-tag">{{ row.user_source }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="用户 ID" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="user-link mono">{{ row.user_id }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="entries" label="记忆" width="90" align="right" />
            <el-table-column prop="pending_review" label="待复核" width="90" align="right" />
            <el-table-column prop="conflicts" label="冲突" width="90" align="right" />
            <el-table-column label="最近更新" min-width="190">
              <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="" width="90">
              <template #default="{ row }">
                <el-button link type="primary" @click.stop="openUser(row)">查看</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!usersQuery.isLoading.value && !users.length" description="暂无用户" />
          <el-pagination
            v-if="usersTotal > pageSize"
            class="pager"
            layout="prev, pager, next, total"
            :total="usersTotal"
            :page-size="pageSize"
            :current-page="page"
            background
            @current-change="(value: number) => (page = value)"
          />
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="删除记录" name="tombstones">
        <el-card shadow="never" class="filter-card">
          <el-form label-position="top" class="filter-form" @submit.prevent>
            <div class="entry-filter-grid">
              <el-form-item label="关键词">
                <el-input
                  v-model="qInput"
                  clearable
                  placeholder="来源 / 用户 ID"
                  @keyup.enter="searchTombstones"
                  @clear="searchTombstones"
                >
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>
              </el-form-item>
            </div>
            <div class="entry-filter-actions">
              <span class="muted">共 {{ tombstonesTotal }} 条删除记录</span>
              <div class="entry-filter-buttons">
                <el-button type="primary" :icon="Search" @click="searchTombstones">查询</el-button>
                <el-button @click="resetTombstones">重置</el-button>
              </div>
            </div>
          </el-form>
        </el-card>

        <el-card shadow="never">
          <el-table
            v-loading="tombstonesQuery.isLoading.value"
            :data="tombstones"
            size="small"
          >
            <el-table-column label="来源" width="120">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" class="source-tag">{{ row.user_source }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="用户 ID" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="mono">{{ row.user_id }}</span>
              </template>
            </el-table-column>
            <el-table-column label="删除时间" min-width="180">
              <template #default="{ row }">{{ formatDateTime(row.deleted_at) }}</template>
            </el-table-column>
            <el-table-column prop="requested_by" label="操作者" width="130" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag v-if="row.purged_at" type="danger" effect="plain" size="small">
                  已彻底删除
                </el-tag>
                <el-tag v-else type="warning" effect="plain" size="small">可恢复</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="210" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openUser(row)">查看</el-button>
                <el-button
                  v-if="!row.purged_at && auth.can('memory.write')"
                  link
                  type="success"
                  @click="restoreTombstone(row)"
                >
                  恢复
                </el-button>
                <el-button
                  v-if="!row.purged_at && auth.can('memory.delete')"
                  link
                  type="danger"
                  @click="purgeTombstone(row)"
                >
                  彻底删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty
            v-if="!tombstonesQuery.isLoading.value && !tombstones.length"
            description="暂无删除记录"
          />
          <el-pagination
            v-if="tombstonesTotal > pageSize"
            class="pager"
            layout="prev, pager, next, total"
            :total="tombstonesTotal"
            :page-size="pageSize"
            :current-page="tombstonePage"
            background
            @current-change="(value: number) => (tombstonePage = value)"
          />
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.filter-card {
  margin-bottom: 16px;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 0;
}

.user-link {
  color: #409eff;
  cursor: pointer;
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>
