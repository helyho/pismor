<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { api, ApiError } from '@/api/client'
import type { AdminAudit, JobRun, SystemHealth, SystemStats } from '@/api/types'
import { formatDateTime } from '@/utils/format'

const health = ref<SystemHealth | null>(null)
const stats = ref<SystemStats | null>(null)
const jobs = ref<JobRun[]>([])
const audits = ref<AdminAudit[]>([])
const loading = ref(false)
const error = ref('')

const statusLabels: Record<string, string> = {
  active: '生效',
  degraded: '降级',
  pending_review: '待复核',
  orphan_topic: '孤立主题',
  archived: '归档',
}

const maxTrend = computed(() => Math.max(1, ...(stats.value?.review_trend ?? []).map((item) => item.count)))
const maxDimension = computed(() =>
  Math.max(1, ...(stats.value?.by_dimension ?? []).map((item) => item.count)),
)
const maxSource = computed(() => Math.max(1, ...(stats.value?.by_source ?? []).map((item) => item.count)))
const maxTopic = computed(() => Math.max(1, ...(stats.value?.top_topics ?? []).map((item) => item.count)))

function errorText(value: unknown): string {
  if (value instanceof ApiError) {
    const detail = value.detail as any
    if (typeof detail === 'string') return detail
    if (detail?.message) return String(detail.message)
    return JSON.stringify(detail)
  }
  return value instanceof Error ? value.message : String(value)
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function duration(job: JobRun): string {
  if (!job.finished_at) return '运行中'
  const start = new Date(job.started_at).getTime()
  const end = new Date(job.finished_at).getTime()
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '-'
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

function statsSummary(job: JobRun): string {
  if (!job.stats) return ''
  const keys = ['activated', 'merged', 'decayed', 'archived', 'audit_violations', 'flagged']
  return keys
    .filter((key) => job.stats?.[key] !== undefined)
    .map((key) => `${key}: ${job.stats?.[key]}`)
    .join('  ·  ')
}

function rowJob(row: unknown): JobRun {
  return row as JobRun
}

function rowAudit(row: unknown): AdminAudit {
  return row as AdminAudit
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [h, s, j, a] = await Promise.all([
      api<SystemHealth>('/system/health'),
      api<SystemStats>('/system/stats'),
      api<JobRun[]>('/system/jobs?limit=20'),
      api<AdminAudit[]>('/system/audit?limit=50'),
    ])
    health.value = h
    stats.value = s
    jobs.value = j
    audits.value = a
  } catch (e) {
    error.value = errorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="system-view">
    <div class="system-head">
      <div>
        <h2>系统</h2>
        <p class="muted">健康检查、日批运行记录、复核趋势与后台操作审计。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" class="gap" />
    <el-alert
      v-if="health && !health.db_ok"
      type="error"
      :title="`数据库异常：${health.error || health.integrity}`"
      show-icon
      :closable="false"
      class="gap"
    />

    <div v-if="health" class="stat-grid">
      <el-card shadow="never" class="stat-card">
        <div class="stat-label">数据库</div>
        <div class="stat-value" :class="health.db_ok ? 'ok' : 'bad'">
          {{ health.db_ok ? '正常' : '异常' }}
        </div>
        <div class="stat-note">{{ health.integrity }} · {{ health.journal_mode }}</div>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-label">记忆 / 用户</div>
        <div class="stat-value">{{ health.entries }} / {{ health.users }}</div>
        <div class="stat-note">待复核 {{ health.pending_review }} · 删除记录 {{ health.tombstones }}</div>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-label">数据库 / WAL</div>
        <div class="stat-value">{{ formatBytes(health.db_bytes) }}</div>
        <div class="stat-note">WAL {{ formatBytes(health.wal_bytes) }} · SQLite {{ health.sqlite_version }}</div>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-label">最近记忆更新</div>
        <div class="stat-value small">{{ formatDateTime(health.updated_at) }}</div>
        <div class="stat-note">时区 UTC</div>
      </el-card>
    </div>

    <el-card shadow="never" class="gap">
      <template #header>
        <div class="card-head">
          <span>日批运行记录</span>
          <span class="muted">最近 20 次</span>
        </div>
      </template>
      <el-table :data="jobs" stripe>
        <el-table-column label="开始" width="190">
          <template #default="{ row }">{{ formatDateTime(rowJob(row).started_at) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">{{ duration(rowJob(row)) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="rowJob(row).ok === true ? 'success' : rowJob(row).ok === false ? 'danger' : 'info'"
            >
              {{ rowJob(row).ok === true ? '成功' : rowJob(row).ok === false ? '失败' : '运行中' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="统计" min-width="320" show-overflow-tooltip>
          <template #default="{ row }">{{ statsSummary(rowJob(row)) }}</template>
        </el-table-column>
        <el-table-column label="错误" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ rowJob(row).error || '' }}</template>
        </el-table-column>
        <template #empty>
          <span class="muted">尚无运行记录；服务启动并执行日批后生成。</span>
        </template>
      </el-table>
    </el-card>

    <div v-if="stats" class="split-grid">
      <el-card shadow="never">
        <template #header>状态分布</template>
        <el-table :data="stats.by_status" size="small">
          <el-table-column label="状态" width="120">
            <template #default="{ row }">{{ statusLabels[row.status] || row.status }}</template>
          </el-table-column>
          <el-table-column prop="count" label="记忆" width="80" />
        </el-table>

        <template #footer>
          <div class="bar-list">
            <div v-for="item in stats.by_status" :key="item.status" class="bar-row">
              <span class="bar-label">{{ statusLabels[item.status] || item.status }}</span>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ width: `${(item.count / Math.max(1, health?.entries ?? 1)) * 100}%` }"
                />
              </div>
              <span class="bar-count">{{ item.count }}</span>
            </div>
          </div>
        </template>
      </el-card>

      <el-card shadow="never">
        <div class="bar-list">
          <div v-for="item in stats.pending_backlog" :key="item.bucket" class="bar-row">
            <span class="bar-label">{{ item.bucket }}</span>
            <div class="bar-track">
              <div
                class="bar-fill warn"
                :style="{ width: `${(item.count / Math.max(1, health?.pending_review ?? 1)) * 100}%` }"
              />
            </div>
            <span class="bar-count">{{ item.count }}</span>
          </div>
          <div v-if="!stats.pending_backlog.length" class="muted">暂无待复核记忆</div>
        </div>

        <template #header>
          <div class="card-head">
            <span>待复核积压</span>
            <span class="muted">按记忆的最近更新时间</span>
          </div>
        </template>
      </el-card>
    </div>

    <el-card v-if="stats" shadow="never" class="gap">
      <template #header>最近 30 天复核处理量</template>
      <div class="trend">
        <div v-for="item in stats.review_trend" :key="item.date" class="trend-col">
          <div class="trend-bar-wrap">
            <div
              class="trend-bar"
              :style="{ height: `${(item.count / maxTrend) * 100}%` }"
              :title="`${item.date}: ${item.count}`"
            />
          </div>
          <span class="trend-label">{{ item.date.slice(5) }}</span>
        </div>
      </div>
    </el-card>

    <div v-if="stats" class="split-grid">
      <el-card shadow="never">
        <template #header>维度分布</template>
        <div class="bar-list">
          <div v-for="item in stats.by_dimension" :key="item.dimension" class="bar-row">
            <span class="bar-label">{{ item.dimension }}</span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: `${(item.count / maxDimension) * 100}%` }" />
            </div>
            <span class="bar-count">{{ item.count }}</span>
          </div>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header>来源分布</template>
        <div class="bar-list">
          <div v-for="item in stats.by_source" :key="item.source" class="bar-row">
            <span class="bar-label">{{ item.source }}</span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: `${(item.count / maxSource) * 100}%` }" />
            </div>
            <span class="bar-count">{{ item.count }}</span>
          </div>
        </div>
      </el-card>
    </div>

    <el-card v-if="stats" shadow="never" class="gap">
      <template #header>Top 主题（生效/降级）</template>
      <div class="bar-list">
        <div v-for="item in stats.top_topics" :key="`${item.dimension}-${item.topic}`" class="bar-row">
          <span class="bar-label">{{ item.dimension }} / {{ item.topic }}</span>
          <div class="bar-track">
            <div class="bar-fill good" :style="{ width: `${(item.count / maxTopic) * 100}%` }" />
          </div>
          <span class="bar-count">{{ item.count }}</span>
        </div>
        <div v-if="!stats.top_topics.length" class="muted">暂无生效记忆</div>
      </div>
    </el-card>

    <el-card shadow="never" class="gap">
      <template #header>
        <div class="card-head">
          <span>后台操作审计</span>
          <span class="muted">最近 50 条</span>
        </div>
      </template>
      <el-table :data="audits" stripe>
        <el-table-column label="时间" width="190">
          <template #default="{ row }">{{ formatDateTime(rowAudit(row).ts) }}</template>
        </el-table-column>
        <el-table-column label="操作者" width="130">
          <template #default="{ row }">{{ rowAudit(row).actor }}</template>
        </el-table-column>
        <el-table-column label="动作" width="160">
          <template #default="{ row }">{{ rowAudit(row).action }}</template>
        </el-table-column>
        <el-table-column label="对象" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ rowAudit(row).target }}</template>
        </el-table-column>
        <el-table-column label="详情" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ rowAudit(row).detail || '' }}</template>
        </el-table-column>
        <template #empty>
          <span class="muted">尚无管理操作记录。</span>
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.system-view {
  max-width: 1120px;
}
.system-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}
.system-head h2 {
  margin: 0 0 4px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.gap {
  margin-top: 12px;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.stat-card {
  min-height: 108px;
}
.stat-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.stat-value {
  margin-top: 8px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.2;
}
.stat-value.small {
  font-size: 16px;
  word-break: break-all;
}
.stat-value.ok {
  color: var(--el-color-success);
}
.stat-value.bad {
  color: var(--el-color-danger);
}
.stat-note {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.split-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}
.bar-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bar-row {
  display: grid;
  grid-template-columns: 150px minmax(0, 1fr) 44px;
  gap: 10px;
  align-items: center;
  font-size: 13px;
}
.bar-label {
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-track {
  height: 8px;
  border-radius: 999px;
  background: var(--el-fill-color);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--el-color-primary);
  min-width: 2px;
}
.bar-fill.warn {
  background: var(--el-color-warning);
}
.bar-fill.good {
  background: var(--el-color-success);
}
.bar-count {
  text-align: right;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
.trend {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 140px;
  padding-top: 8px;
}
.trend-col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  justify-content: flex-end;
  gap: 4px;
}
.trend-bar-wrap {
  flex: 1;
  display: flex;
  align-items: flex-end;
}
.trend-bar {
  width: 100%;
  border-radius: 3px 3px 0 0;
  background: var(--el-color-primary);
  min-height: 1px;
}
.trend-label {
  font-size: 10px;
  color: var(--el-text-color-secondary);
  text-align: center;
  transform: rotate(-45deg);
  white-space: nowrap;
}
@media (max-width: 900px) {
  .stat-grid,
  .split-grid {
    grid-template-columns: 1fr;
  }
  .bar-row {
    grid-template-columns: 100px minmax(0, 1fr) 36px;
  }
}
</style>
