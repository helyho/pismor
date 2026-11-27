<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { ApiError, createEntry, listEntryTopics, validateCreateEntry } from '@/api/client'
import type {
  EntryConflictRow,
  EntryCreatePayload,
  EntryCreateResult,
  EntryValidateResult,
  EntryWriteErrorDetail,
  FieldError,
} from '@/api/types'
import ConflictPanel from './ConflictPanel.vue'

/** 传入即预填并锁定 user_source/user_id（用户详情页场景）；列表页不传则两者可填。 */
interface CreateTarget {
  user_source?: string
  user_id?: string
}

const props = defineProps<{
  modelValue: boolean
  target?: CreateTarget | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [result: EntryCreateResult]
  /** 409 同键冲突时“去编辑这条”：existing 携带冲突行（无用户上下文），target 取表单中的来源/用户。 */
  gotoEdit: [existing: EntryConflictRow, target: { user_source: string; user_id: string }]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const saving = ref(false)
const checking = ref(false)
const topics = ref<Record<string, string[]>>({})
const validate = ref<EntryValidateResult | null>(null)
// HTTP 错误反馈（ApiError.detail 为 EntryWriteErrorDetail 的各状态子集）
const httpErrors = ref<Array<FieldError | string> | null>(null)
const httpWarnings = ref<string[] | null>(null)
const httpMessage = ref('')
const existing = ref<EntryConflictRow | null>(null)
const isConflict = ref(false) // 409：保留表单，展示 existing 摘要 + 跳编辑
const isServerError = ref(false) // 503：保留表单，可重试

const locked = computed(() => Boolean(props.target?.user_source || props.target?.user_id))

function emptyForm(): EntryCreatePayload {
  return {
    user_source: props.target?.user_source ?? '',
    user_id: props.target?.user_id ?? '',
    dimension: '',
    topic: '',
    summary: '',
    body: '',
    quote: '',
    confidence: 1,
    origin_ref: null,
  }
}

const form = ref<EntryCreatePayload>(emptyForm())
const dimensionOptions = computed(() => Object.keys(topics.value))
const topicOptions = computed(() => topics.value[form.value.dimension] ?? [])

const originRefText = computed({
  get: () => form.value.origin_ref ?? '',
  set: (value: string) => {
    form.value.origin_ref = value.trim() ? value : null
  },
})

// 校验结果优先，其次 HTTP 错误（400/422/409）的字段级 errors
const panelErrors = computed<Array<FieldError | string> | null>(() => {
  if (validate.value) return validate.value.errors
  if (httpErrors.value) return httpErrors.value
  return null
})

const panelWarnings = computed<string[] | null>(() => {
  if (validate.value) return validate.value.warnings
  return httpWarnings.value
})

function resetFeedback(): void {
  validate.value = null
  httpErrors.value = null
  httpWarnings.value = null
  httpMessage.value = ''
  existing.value = null
  isConflict.value = false
  isServerError.value = false
}

async function loadTopics(): Promise<void> {
  if (dimensionOptions.value.length) return
  try {
    topics.value = await listEntryTopics()
  } catch {
    topics.value = {}
    httpMessage.value = '词表加载失败，请关闭后重开；字段仍可先行填写'
  }
}

function onDimensionChange(): void {
  form.value.topic = ''
}

function fillQuoteFromBody(): void {
  form.value.quote = form.value.body
}

function applyHttpError(error: unknown): void {
  if (!(error instanceof ApiError)) {
    httpMessage.value = error instanceof Error ? error.message : String(error)
    return
  }
  // 类型化断言（EntryWriteErrorDetail），替代 as any：400/409/422/503 各字段均可选
  const detail = error.detail as EntryWriteErrorDetail | string | null
  if (error.status === 409) {
    isConflict.value = true
    if (detail && typeof detail !== 'string') {
      httpErrors.value = Array.isArray(detail.errors) ? detail.errors : []
      existing.value = detail.existing ?? null
    } else {
      httpMessage.value = typeof detail === 'string' ? detail : error.message
    }
    return
  }
  if (error.status === 503) {
    isServerError.value = true
    httpMessage.value =
      detail && typeof detail !== 'string' && detail.message
        ? String(detail.message)
        : error.message
    return
  }
  // 400 / 422：字段级校验错误 + 警告（红色提示由 ConflictPanel 承担）
  if (detail && typeof detail !== 'string') {
    if (Array.isArray(detail.errors) && detail.errors.length) {
      httpErrors.value = detail.errors
      httpWarnings.value = Array.isArray(detail.warnings) ? detail.warnings : []
      return
    }
    if (detail.message) {
      httpMessage.value = String(detail.message)
      return
    }
  }
  httpMessage.value = typeof detail === 'string' ? detail : error.message
}

async function checkCreate(): Promise<void> {
  checking.value = true
  resetFeedback()
  try {
    validate.value = await validateCreateEntry(form.value)
    if (validate.value.ok) ElMessage.success('本地检查通过；同键冲突保存时还会再次校验')
  } catch (error) {
    applyHttpError(error)
  } finally {
    checking.value = false
  }
}

async function save(): Promise<void> {
  if (saving.value) return
  saving.value = true
  resetFeedback()
  try {
    const result = await createEntry(form.value)
    ElMessage.success('记忆已创建，立即参与注入')
    visible.value = false
    emit('saved', result)
  } catch (error) {
    // 400/409/422/503 均保留表单；503 时 saving 在 finally 复位，保存按钮可直接重试
    applyHttpError(error)
  } finally {
    saving.value = false
  }
}

function gotoEdit(): void {
  if (!existing.value) return
  visible.value = false
  emit('gotoEdit', existing.value, {
    user_source: form.value.user_source,
    user_id: form.value.user_id,
  })
}

watch(
  () => [props.modelValue, props.target] as const,
  ([open]) => {
    if (!open) return
    form.value = emptyForm()
    resetFeedback()
    void loadTopics()
  },
  { immediate: true },
)
</script>

<template>
  <el-drawer
    v-model="visible"
    :with-header="false"
    size="720px"
    class="entry-create-drawer"
    :close-on-click-modal="false"
  >
    <div class="create-shell">
      <header class="create-head">
        <div>
          <h3>新建记忆</h3>
          <p class="create-subtitle">
            保存后立即参与注入；同键（来源 + 用户 + 维度 + 主题）已有记忆会被拒绝。
          </p>
        </div>
        <el-button text @click="visible = false">关闭</el-button>
      </header>

      <div class="create-body">
        <el-alert
          v-if="isServerError"
          :title="`服务暂不可用，可稍后重试：${httpMessage}`"
          type="warning"
          :closable="false"
          show-icon
          class="create-alert"
        />
        <el-alert
          v-else-if="httpMessage"
          :title="httpMessage"
          type="error"
          :closable="false"
          show-icon
          class="create-alert"
        />

        <ConflictPanel :errors="panelErrors" :warnings="panelWarnings" />

        <div v-if="isConflict && existing" class="existing-box">
          <h4>同键记忆已存在</h4>
          <div class="existing-row">
            <span class="mono">{{ existing.id }}</span>
            <el-tag size="small">{{ existing.status }}</el-tag>
            <span class="existing-text">{{ existing.summary }}</span>
          </div>
          <el-button type="primary" size="small" @click="gotoEdit">去编辑这条</el-button>
        </div>

        <el-form label-position="top" class="create-form">
          <div class="form-grid">
            <el-form-item label="来源">
              <el-input
                v-model="form.user_source"
                :disabled="locked"
                maxlength="128"
                placeholder="如 webchat"
              />
            </el-form-item>
            <el-form-item label="用户 ID">
              <el-input
                v-model="form.user_id"
                :disabled="locked"
                maxlength="128"
                class="mono"
                placeholder="用户标识"
              />
            </el-form-item>
            <el-form-item label="维度">
              <el-select
                v-model="form.dimension"
                filterable
                placeholder="选择维度"
                style="width: 100%"
                @change="onDimensionChange"
              >
                <el-option
                  v-for="item in dimensionOptions"
                  :key="item"
                  :label="item"
                  :value="item"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="主题">
              <el-select
                v-model="form.topic"
                filterable
                allow-create
                default-first-option
                :placeholder="form.dimension ? '选择或输入主题' : '请先选择维度'"
                style="width: 100%"
              >
                <el-option v-for="item in topicOptions" :key="item" :label="item" :value="item" />
              </el-select>
            </el-form-item>
          </div>

          <el-form-item label="摘要">
            <el-input
              v-model="form.summary"
              type="textarea"
              :rows="2"
              maxlength="20"
              show-word-limit
              placeholder="一句话概括，20 字以内"
            />
          </el-form-item>
          <el-form-item label="正文">
            <el-input v-model="form.body" type="textarea" :rows="5" maxlength="200" show-word-limit />
            <el-button text type="primary" size="small" @click="fillQuoteFromBody">
              用正文填充依据
            </el-button>
          </el-form-item>
          <el-form-item label="依据（用户原话）">
            <el-input
              v-model="form.quote"
              type="textarea"
              :rows="3"
              maxlength="500"
              show-word-limit
              placeholder="依据应为用户原话，不超过 500 字"
            />
          </el-form-item>

          <el-collapse class="advanced-collapse">
            <el-collapse-item title="高级选项" name="advanced">
              <el-form-item label="置信度">
                <el-input-number v-model="form.confidence" :min="0" :max="1" :step="0.01" />
              </el-form-item>
              <el-form-item label="来源引用 origin_ref">
                <el-input v-model="originRefText" placeholder="可选：原始出处，如消息链接" />
              </el-form-item>
            </el-collapse-item>
          </el-collapse>
        </el-form>
      </div>

      <footer class="create-footer">
        <span class="muted">保存后立即参与注入；依据应为用户原话</span>
        <div class="create-footer-actions">
          <el-button @click="visible = false">取消</el-button>
          <el-button :loading="checking" @click="checkCreate">检查冲突</el-button>
          <el-button type="primary" :loading="saving" @click="save">保存</el-button>
        </div>
      </footer>
    </div>
  </el-drawer>
</template>

<style scoped>
.create-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.create-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 24px 16px;
  border-bottom: 1px solid var(--app-border);
}

.create-head h3 {
  margin: 0;
  font-size: 20px;
  letter-spacing: -0.02em;
}

.create-subtitle {
  margin: 5px 0 0;
  color: var(--app-muted);
  font-size: 12px;
}

.create-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
}

.create-alert {
  margin-bottom: 12px;
}

.create-form {
  margin-top: 4px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}

.existing-box {
  margin-bottom: 12px;
  padding: 12px 14px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
}

.existing-box h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

.existing-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.existing-text {
  color: var(--app-muted);
  font-size: 13px;
}

.advanced-collapse {
  margin-top: 4px;
}

.advanced-collapse :deep(.el-collapse-item__header) {
  font-size: 13px;
  color: var(--app-muted);
}

.create-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 24px;
  border-top: 1px solid var(--app-border);
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(8px);
}

.create-footer-actions {
  display: flex;
  gap: 8px;
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .create-footer {
    align-items: flex-end;
    flex-direction: column;
  }
}
</style>
