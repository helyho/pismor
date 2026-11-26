<script setup lang="ts">
import { ref, watch } from 'vue'

interface AliasRow {
  id: number
  alias: string
  canonical: string
}

const props = withDefaults(
  defineProps<{
    modelValue?: Record<string, string> | null
    topics?: string[]
  }>(),
  {
    modelValue: null,
    topics: () => [],
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, string>]
}>()

let seq = 0
const rows = ref<AliasRow[]>([])
// 记住最近一次向父级提交的内容，避免父级回写时把正在输入的空行冲掉。
let lastEmitted = ''

function toRows(value?: Record<string, string> | null): AliasRow[] {
  return Object.entries(value ?? {}).map(([alias, canonical]) => ({
    id: (seq += 1),
    alias,
    canonical: typeof canonical === 'string' ? canonical : '',
  }))
}

function submitted(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const row of rows.value) {
    const alias = row.alias.trim()
    if (!alias || !row.canonical) continue
    out[alias] = row.canonical
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

function setAlias(row: AliasRow, value: unknown): void {
  row.alias = String(value ?? '')
  commit()
}

function setCanonical(row: AliasRow, value: unknown): void {
  row.canonical = String(value ?? '')
  commit()
}

function addRow(): void {
  rows.value.push({ id: (seq += 1), alias: '', canonical: '' })
}

function removeRow(index: number): void {
  rows.value.splice(index, 1)
  commit()
}
</script>

<template>
  <div class="alias-editor">
    <div v-for="(row, index) in rows" :key="row.id" class="alias-row">
      <el-input
        :model-value="row.alias"
        placeholder="自由 topic，如 表格"
        class="alias-row__alias"
        @update:model-value="(value) => setAlias(row, value)"
      />
      <span class="alias-row__arrow">→</span>
      <el-select
        :model-value="row.canonical"
        filterable
        placeholder="白名单 topic"
        class="alias-row__canonical"
        @update:model-value="(value) => setCanonical(row, value)"
      >
        <el-option v-for="topic in props.topics" :key="topic" :label="topic" :value="topic" />
      </el-select>
      <el-button link type="danger" @click="removeRow(index)">删除</el-button>
    </div>
    <div class="alias-actions">
      <el-button size="small" plain :disabled="!props.topics.length" @click="addRow">
        添加别名
      </el-button>
      <span v-if="!props.topics.length" class="alias-hint">先填写 topics 才能配置别名</span>
      <span v-else-if="!rows.length" class="alias-hint">未配置别名；自由 topic 不会被归一到白名单</span>
    </div>
  </div>
</template>

<style scoped>
.alias-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.alias-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.alias-row__alias {
  width: 220px;
}
.alias-row__arrow {
  color: var(--el-text-color-secondary);
}
.alias-row__canonical {
  width: 220px;
}
.alias-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.alias-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
@media (max-width: 720px) {
  .alias-row__alias,
  .alias-row__canonical {
    width: 100%;
  }
}
</style>
