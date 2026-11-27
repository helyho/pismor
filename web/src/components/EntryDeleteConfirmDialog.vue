<script setup lang="ts">
import { computed } from 'vue'

import type { EntryDeletePreview } from '@/api/types'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{
  modelValue: boolean
  preview: EntryDeletePreview | null
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [cascade: boolean]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const statusLabels: Record<string, string> = {
  pending_review: '待复核',
  orphan_topic: '观察区',
  active: '启用',
  degraded: '降级',
  archived: '归档',
}

const cascaded = computed(() => props.preview?.cascaded ?? [])
const total = computed(() => props.preview?.total ?? 1)

function statusLabel(value: string): string {
  return statusLabels[value] ?? value
}

function excerpt(text: string): string {
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="删除记忆"
    width="640px"
    :close-on-click-modal="false"
    append-to-body
  >
    <template v-if="preview">
      <el-alert
        title="物理删除，不可恢复。"
        type="error"
        :closable="false"
        show-icon
        class="delete-alert"
      />
      <el-alert
        v-for="warning in preview.warnings"
        :key="warning"
        :title="warning"
        type="warning"
        :closable="false"
        show-icon
        class="delete-alert"
      />

      <div class="delete-label">目标条目</div>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="维度">
          {{ preview.target.dimension }}
        </el-descriptions-item>
        <el-descriptions-item label="主题">
          {{ preview.target.topic }}
        </el-descriptions-item>
        <el-descriptions-item label="状态">
          {{ statusLabel(preview.target.status) }}
        </el-descriptions-item>
        <el-descriptions-item label="变体">
          {{ preview.target.variant }} #{{ preview.target.variant_seq }}
        </el-descriptions-item>
        <el-descriptions-item label="更新时间" :span="2">
          {{ formatDateTime(preview.target.updated_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="摘要" :span="2">
          {{ excerpt(preview.target.summary) }}
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="cascaded.length" class="delete-cascade">
        <div class="delete-label">
          以下 {{ cascaded.length }} 条同维度同主题的冲突副本将一并删除
        </div>
        <el-table :data="cascaded" size="small" border max-height="240">
          <el-table-column label="变体" width="110">
            <template #default="{ row }">
              {{ row.variant }} #{{ row.variant_seq }}
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">{{ statusLabel(row.status) }}</template>
          </el-table-column>
          <el-table-column label="摘要" min-width="220">
            <template #default="{ row }">{{ excerpt(row.summary) }}</template>
          </el-table-column>
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <p class="delete-hint">取消则不会删除任何条目。</p>
    </template>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        v-if="cascaded.length"
        :loading="loading"
        @click="emit('confirm', false)"
      >
        仅删除当前条目
      </el-button>
      <el-button type="danger" :loading="loading" @click="emit('confirm', true)">
        {{ cascaded.length ? `全部删除（${total} 条）` : '删除' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.delete-alert {
  margin-bottom: 10px;
}

.delete-label {
  margin: 14px 0 8px;
  font-weight: 600;
}

.delete-cascade .delete-label {
  margin-top: 18px;
}

.delete-hint {
  margin: 12px 0 0;
  color: var(--app-muted);
  font-size: 12px;
}
</style>
