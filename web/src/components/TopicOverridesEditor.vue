<script setup lang="ts">
import { ref, watch } from 'vue'

type SensitiveOption = '' | 'true' | 'false' | 'partial'

interface OverrideRow {
  id: number
  topic: string
  sensitive: SensitiveOption
  note: string
  // 当前版本不读取的旧字段原样保留，避免编辑时静默丢数据。
  extra: Record<string, unknown>
}

const props = withDefaults(
  defineProps<{
    modelValue?: Record<string, unknown> | null
    topics?: string[]
  }>(),
  {
    modelValue: null,
    topics: () => [],
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, unknown>]
}>()

let seq = 0
const rows = ref<OverrideRow[]>([])
let lastEmitted = ''

function sensitiveValue(raw: unknown): SensitiveOption {
  if (raw === true) return 'true'
  if (raw === 'partial') return 'partial'
  // 历史误配的字符串 "false" 在运行期按真值处理，这里如实显示为 true。
  if (raw) return 'true'
  return ''
}

function toRows(value?: Record<string, unknown> | null): OverrideRow[] {
  const out: OverrideRow[] = []
  for (const [topic, raw] of Object.entries(value ?? {})) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    const entry = raw as Record<string, unknown>
    const extra: Record<string, unknown> = {}
    for (const [key, item] of Object.entries(entry)) {
      if (key !== 'sensitive' && key !== 'note') extra[key] = item
    }
    out.push({
      id: (seq += 1),
      topic,
      sensitive: sensitiveValue(entry.sensitive),
      note: typeof entry.note === 'string' ? entry.note : '',
      extra,
    })
  }
  return out
}

function submitted(): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const row of rows.value) {
    const topic = row.topic.trim()
    if (!topic) continue
    const entry: Record<string, unknown> = { ...row.extra }
    if (row.sensitive === 'true') entry.sensitive = true
    else if (row.sensitive === 'false') entry.sensitive = false
    else if (row.sensitive === 'partial') entry.sensitive = 'partial'
    const note = row.note.trim()
    if (note) entry.note = note
    if (Object.keys(entry).length) out[topic] = entry
  }
  return out
}

function commit(): void {
  const value = submitted()
  lastEmitted = JSON.stringify(value)
  emit('update:modelValue', value)
}

watch(
  () => props.modelValue,
  (value) => {
    const signature = JSON.stringify(value ?? {})
    if (signature === lastEmitted) return
    rows.value = toRows(value)
    lastEmitted = signature
  },
  { immediate: true, deep: true },
)

function setTopic(row: OverrideRow, value: unknown): void {
  row.topic = String(value ?? '')
  commit()
}

function setSensitive(row: OverrideRow, value: unknown): void {
  row.sensitive =
    value === 'true' || value === 'false' || value === 'partial' ? value : ''
  commit()
}

function setNote(row: OverrideRow, value: unknown): void {
  row.note = String(value ?? '')
  commit()
}

function addRow(): void {
  rows.value.push({ id: (seq += 1), topic: '', sensitive: '', note: '', extra: {} })
}

function removeRow(index: number): void {
  rows.value.splice(index, 1)
  commit()
}
</script>

<template>
  <div class="override-editor">
    <div v-for="(row, index) in rows" :key="row.id" class="override-row">
      <el-select
        :model-value="row.topic"
        filterable
        placeholder="主题"
        class="override-row__topic"
        @update:model-value="(value) => setTopic(row, value)"
      >
        <el-option v-for="topic in props.topics" :key="topic" :label="topic" :value="topic" />
      </el-select>
      <el-select
        :model-value="row.sensitive"
        placeholder="继承（不设置）"
        clearable
        class="override-row__sensitive"
        @update:model-value="(value) => setSensitive(row, value)"
      >
        <el-option label="继承（不设置）" value="" />
        <el-option label="true（敏感）" value="true" />
        <el-option label="partial（敏感，语义区分）" value="partial" />
        <el-option label="false（不敏感）" value="false" />
      </el-select>
      <el-input
        :model-value="row.note"
        placeholder="note，如：精确位置需脱敏到城市级"
        class="override-row__note"
        @update:model-value="(value) => setNote(row, value)"
      />
      <el-button link type="danger" @click="removeRow(index)">删除</el-button>
    </div>
    <div class="override-actions">
      <el-button size="small" plain :disabled="!props.topics.length" @click="addRow">
        添加覆盖
      </el-button>
      <span v-if="!props.topics.length" class="override-hint">先填写 topics 才能配置覆盖</span>
      <span v-else-if="!rows.length" class="override-hint">未配置覆盖；主题继承维度级 sensitive</span>
    </div>
  </div>
</template>

<style scoped>
.override-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.override-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.override-row__topic {
  width: 180px;
}
.override-row__sensitive {
  width: 200px;
}
.override-row__note {
  flex: 1;
  min-width: 220px;
}
.override-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.override-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
@media (max-width: 720px) {
  .override-row__topic,
  .override-row__sensitive,
  .override-row__note {
    width: 100%;
    min-width: 0;
  }
}
</style>
