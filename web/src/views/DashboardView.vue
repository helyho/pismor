<script setup lang="ts">
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'

import { api } from '@/api/client'
import type { Overview } from '@/api/types'
import { formatDateTime } from '@/utils/format'

const { data, isLoading, isError, error, refetch } = useQuery<Overview>({
  queryKey: ['overview'],
  queryFn: () => api<Overview>('/overview'),
})

const statusLabels: Record<string, string> = {
  pending_review: '待复核',
  orphan_topic: '观察区',
  active: '启用',
  degraded: '降级',
  archived: '归档',
}

const stats = computed(() => {
  if (!data.value) return []
  return [
    { label: '用户', value: data.value.users },
    { label: '记忆', value: data.value.entries },
    { label: '待复核', value: data.value.pending_review },
    { label: '冲突', value: data.value.conflicts },
    { label: '删除记录', value: data.value.tombstones },
    { label: '数据库', value: formatBytes(data.value.db_bytes + data.value.wal_bytes) },
  ]
})

function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>概览</h2>
      <el-button :loading="isLoading" @click="refetch()">刷新</el-button>
    </div>

    <el-alert
      v-if="isError"
      type="error"
      show-icon
      :title="`加载概览失败：${String(error)}`"
      :closable="false"
    />

    <el-skeleton v-if="isLoading" :rows="4" animated />

    <template v-else-if="data">
      <el-row :gutter="16" class="stat-grid">
        <el-col v-for="item in stats" :key="item.label" :xs="12" :sm="8" :lg="4">
          <el-card shadow="never" class="stat-card">
            <div class="stat-label">{{ item.label }}</div>
            <div class="stat-value">{{ item.value }}</div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16">
        <el-col :sm="12">
          <el-card shadow="never" header="按状态">
            <el-table :data="data.by_status" size="small">
              <el-table-column label="状态">
                <template #default="{ row }">{{ statusLabel(row.status) }}</template>
              </el-table-column>
              <el-table-column prop="count" label="记忆数" width="100" align="right" />
            </el-table>
          </el-card>
        </el-col>
        <el-col :sm="12">
          <el-card shadow="never" header="按维度">
            <el-table :data="data.by_dimension" size="small">
              <el-table-column prop="dimension" label="维度" />
              <el-table-column prop="count" label="记忆数" width="100" align="right" />
            </el-table>
          </el-card>
        </el-col>
      </el-row>

      <p class="muted">最近更新：{{ formatDateTime(data.updated_at) }}</p>
    </template>
  </div>
</template>
