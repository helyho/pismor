<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { api, ApiError } from '@/api/client'
import type {
  EntryEditCheck,
  EntryEditPayload,
  EntryFacets,
  EntryStatus,
  ReviewDetail,
} from '@/api/types'

interface Target {
  user_source: string
  user_id: string
  entry_id: string
}

const props = defineProps<{
  modelValue: boolean
  target: Target | null
  facets?: EntryFacets | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const loading = ref(false)
const checking = ref(false)
const saving = ref(false)
const error = ref('')
const check = ref<EntryEditCheck | null>(null)

const statusLabels: Record<string, string> = {
  pending_review: '待复核',
  orphan_topic: '观察区',
  active: '启用',
  degraded: '降级',
  archived: '归档',
}

const statusOptions: EntryStatus[] = [
  'pending_review',
  'orphan_topic',
  'active',
  'degraded',
  'archived',
]

function emptyForm(): EntryEditPayload {
  return {
    user_source: '',
    user_id: '',
    entry_id: '',
    expected_version: '',
    new_dimension: '',
    new_topic: '',
    confidence: 0.5,
    status: 'pending_review',
    summary: '',
    body: '',
    degraded_reason: '',
  }
}

const form = ref<EntryEditPayload>(emptyForm())

function statusLabel(value: string): string {
  return statusLabels[value] ?? value
}

function formatError(value: unknown): string {
  if (value instanceof ApiError) {
    const detail = value.detail as any
    if (detail?.errors && Array.isArray(detail.errors)) return detail.errors.join('\n')
    if (typeof detail === 'string') return detail
    if (detail?.message) return String(detail.message)
    return JSON.stringify(detail)
  }
  return value instanceof Error ? value.message : String(value)
}

async function load(): Promise<void> {
  if (!props.target) return
  loading.value = true
  error.value = ''
  check.value = null
  try {
    const params = new URLSearchParams({
      user_source: props.target.user_source,
      user_id: props.target.user_id,
      entry_id: props.target.entry_id,
    })
    const detail = await api<ReviewDetail>(`/entries/detail?${params.toString()}`)
    form.value = {
      user_source: detail.entry.user_source,
      user_id: detail.entry.user_id,
      entry_id: detail.entry.id,
      expected_version: detail.entry.updated_at,
      new_dimension: detail.entry.dimension,
      new_topic: detail.entry.topic,
      confidence: detail.entry.confidence,
      status: detail.entry.status as EntryStatus,
      summary: detail.entry.summary,
      body: detail.entry.body,
      degraded_reason: detail.entry.degraded_reason ?? '',
    }
  } catch (e) {
    error.value = formatError(e)
  } finally {
    loading.value = false
  }
}

async function checkEdit(): Promise<boolean> {
  error.value = ''
  checking.value = true
  try {
    check.value = await api<EntryEditCheck>('/entries/validate-edit', {
      method: 'POST',
      body: JSON.stringify(form.value),
    })
    return check.value.ok
  } catch (e) {
    error.value = formatError(e)
    return false
  } finally {
    checking.value = false
  }
}

async function saveEdit(): Promise<void> {
  const ok = await checkEdit()
  if (!ok) return
  saving.value = true
  error.value = ''
  try {
    await api('/entries', {
      method: 'PATCH',
      body: JSON.stringify(form.value),
    })
    ElMessage.success('记忆已更新')
    visible.value = false
    emit('saved')
  } catch (e) {
    error.value = formatError(e)
    if (e instanceof ApiError) {
      const detail = e.detail as any
      if (detail && Array.isArray(detail.errors)) {
        check.value = {
          ok: false,
          errors: detail.errors,
          warnings: Array.isArray(detail.warnings) ? detail.warnings : [],
          conflicts: Array.isArray(detail.conflicts) ? detail.conflicts : [],
        }
      }
    }
  } finally {
    saving.value = false
  }
}

watch(
  () => [props.modelValue, props.target] as const,
  ([open]) => {
    if (open) void load()
  },
  { immediate: true },
)
</script>

<template>
  <el-drawer
    v-model="visible"
    :with-header="false"
    size="720px"
    class="entry-edit-drawer"
    :close-on-click-modal="false"
  >
    <div v-loading="loading" class="edit-shell">
      <header class="edit-head">
        <div>
          <h3>编辑记忆</h3>
          <p class="edit-subtitle">
            {{ form.new_dimension || '维度' }} / {{ form.new_topic || '主题' }}
          </p>
        </div>
        <el-button text @click="visible = false">关闭</el-button>
      </header>

      <div class="edit-body">
        <el-alert
          v-if="error"
          :title="error"
          type="error"
          :closable="false"
          show-icon
          class="edit-alert"
        />

        <el-form label-position="top" class="edit-form">
          <div class="form-grid">
            <el-form-item label="来源">
              <el-input :model-value="form.user_source" disabled />
            </el-form-item>
            <el-form-item label="用户 ID">
              <el-input :model-value="form.user_id" disabled class="mono" />
            </el-form-item>
            <el-form-item label="维度">
              <el-select v-model="form.new_dimension" filterable style="width: 100%">
                <el-option
                  v-for="item in facets?.dimensions ?? []"
                  :key="item.value"
                  :label="item.value"
                  :value="item.value"
                />
                <el-option
                  v-if="
                    form.new_dimension &&
                    !(facets?.dimensions ?? []).some((item) => item.value === form.new_dimension)
                  "
                  :label="form.new_dimension"
                  :value="form.new_dimension"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="主题">
              <el-input v-model="form.new_topic" />
            </el-form-item>
            <el-form-item label="状态">
              <el-select v-model="form.status" style="width: 100%">
                <el-option
                  v-for="item in statusOptions"
                  :key="item"
                  :label="statusLabel(item)"
                  :value="item"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="置信度">
              <el-input-number v-model="form.confidence" :min="0" :max="1" :step="0.01" />
            </el-form-item>
            <el-form-item label="降级原因">
              <el-input v-model="form.degraded_reason" placeholder="仅状态为降级时填写" />
            </el-form-item>
          </div>

          <el-form-item label="摘要">
            <el-input v-model="form.summary" type="textarea" :rows="3" />
          </el-form-item>
          <el-form-item label="正文">
            <el-input v-model="form.body" type="textarea" :rows="10" />
          </el-form-item>
        </el-form>

        <el-alert
          v-if="check?.errors.length"
          :title="check.errors.join('；')"
          type="error"
          :closable="false"
          show-icon
          class="edit-alert"
        />
        <el-alert
          v-if="check?.warnings.length"
          :title="check.warnings.join('；')"
          type="warning"
          :closable="false"
          show-icon
          class="edit-alert"
        />

        <div v-if="check?.conflicts.length" class="conflict-box">
          <h4>目标键已有记忆</h4>
          <el-table :data="check.conflicts" size="small">
            <el-table-column prop="id" label="记忆" min-width="220" />
            <el-table-column prop="status" label="状态" width="100" />
            <el-table-column prop="summary" label="摘要" min-width="220" />
          </el-table>
        </div>
      </div>

      <footer class="edit-footer">
        <span class="muted">保存前会自动检查目标键冲突</span>
        <div class="edit-footer-actions">
          <el-button @click="visible = false">取消</el-button>
          <el-button :loading="checking" @click="checkEdit">检查冲突</el-button>
          <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
        </div>
      </footer>
    </div>
  </el-drawer>
</template>

<style scoped>
.edit-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.edit-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 24px 16px;
  border-bottom: 1px solid var(--app-border);
}

.edit-head h3 {
  margin: 0;
  font-size: 20px;
  letter-spacing: -0.02em;
}

.edit-subtitle {
  margin: 5px 0 0;
  color: var(--app-muted);
  font-size: 12px;
}

.edit-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
}

.edit-alert {
  margin-bottom: 12px;
}

.edit-form {
  margin-top: 4px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}

.edit-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 24px;
  border-top: 1px solid var(--app-border);
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(8px);
}

.edit-footer-actions {
  display: flex;
  gap: 8px;
}

.conflict-box {
  margin-top: 12px;
}

.conflict-box h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .edit-footer {
    align-items: flex-end;
    flex-direction: column;
  }
}
</style>
