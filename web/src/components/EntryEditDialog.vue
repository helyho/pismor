<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { api, ApiError } from '@/api/client'
import type {
  EntryDeletePreview,
  EntryEditCheck,
  EntryEditPayload,
  EntryFacets,
  EntryStatus,
  ReviewDetail,
} from '@/api/types'
import { useAuthStore } from '@/auth'
import { formatError } from '@/utils/format'
import ConflictPanel from './ConflictPanel.vue'
import EntryDeleteConfirmDialog from './EntryDeleteConfirmDialog.vue'

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
  deleted: []
}>()

const auth = useAuthStore()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const loading = ref(false)
const checking = ref(false)
const saving = ref(false)
const error = ref('')
const check = ref<EntryEditCheck | null>(null)

const previewing = ref(false)
const deleting = ref(false)
const deleteVisible = ref(false)
const deletePreview = ref<EntryDeletePreview | null>(null)

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

async function prepareDelete(): Promise<void> {
  if (!props.target) return
  previewing.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({
      user_source: props.target.user_source,
      user_id: props.target.user_id,
      entry_id: props.target.entry_id,
      cascade: 'true',
    })
    deletePreview.value = await api<EntryDeletePreview>(
      `/entries/delete-preview?${params.toString()}`,
    )
    deleteVisible.value = true
  } catch (e) {
    error.value = formatError(e)
  } finally {
    previewing.value = false
  }
}

async function confirmDelete(cascade: boolean): Promise<void> {
  if (!props.target || !deletePreview.value) return
  deleting.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({
      user_source: props.target.user_source,
      user_id: props.target.user_id,
      entry_id: props.target.entry_id,
      cascade: String(cascade),
      expected_version: deletePreview.value.target.updated_at,
    })
    const result = await api<{ deleted: string[] }>(`/entries?${params.toString()}`, {
      method: 'DELETE',
    })
    ElMessage.success(`已删除 ${result.deleted.length} 条`)
    deleteVisible.value = false
    deletePreview.value = null
    visible.value = false
    emit('deleted')
  } catch (e) {
    error.value = formatError(e)
    if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
      deleteVisible.value = false
    }
  } finally {
    deleting.value = false
  }
}

watch(
  () => [props.modelValue, props.target] as const,
  ([open]) => {
    if (open) {
      void load()
    } else {
      deleteVisible.value = false
      deletePreview.value = null
    }
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

        <ConflictPanel
          :errors="check?.errors"
          :warnings="check?.warnings"
          :conflicts="check?.conflicts"
        />
      </div>

      <footer class="edit-footer">
        <div class="edit-footer-left">
          <el-button
            v-if="auth.can('memory.delete')"
            type="danger"
            text
            :loading="previewing"
            @click="prepareDelete"
          >
            删除这条记忆
          </el-button>
          <span class="muted">保存前会自动检查目标键冲突</span>
        </div>
        <div class="edit-footer-actions">
          <el-button @click="visible = false">取消</el-button>
          <el-button :loading="checking" @click="checkEdit">检查冲突</el-button>
          <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
        </div>
      </footer>
    </div>
  </el-drawer>

  <EntryDeleteConfirmDialog
    v-model="deleteVisible"
    :preview="deletePreview"
    :loading="deleting"
    @confirm="confirmDelete"
  />
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

.edit-footer-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.edit-footer-actions {
  display: flex;
  gap: 8px;
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
