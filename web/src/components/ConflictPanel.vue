<script setup lang="ts">
import { computed } from 'vue'
import type { EntryConflictRow, FieldError } from '@/api/types'
import { formatErrorItem } from '@/utils/format'

// props 接受后端原样结构：errors 可为 string[]（编辑校验）或 {field,code,message}[]（创建校验），
// 渲染前经 formatErrorItem 归一；conflicts 只展示 记忆/状态/摘要 三列（与编辑对话框现状一致）。
const props = defineProps<{
  errors?: Array<string | FieldError> | null
  warnings?: string[] | null
  conflicts?: EntryConflictRow[] | null
}>()

const errorItems = computed(() => props.errors ?? [])
const warningItems = computed(() => props.warnings ?? [])
const conflictRows = computed(() => props.conflicts ?? [])
</script>

<template>
  <el-alert
    v-if="errorItems.length"
    :title="errorItems.map(formatErrorItem).join('；')"
    type="error"
    :closable="false"
    show-icon
    class="edit-alert"
  />
  <el-alert
    v-if="warningItems.length"
    :title="warningItems.join('；')"
    type="warning"
    :closable="false"
    show-icon
    class="edit-alert"
  />
  <div v-if="conflictRows.length" class="conflict-box">
    <h4>目标键已有记忆</h4>
    <el-table :data="conflictRows" size="small">
      <el-table-column prop="id" label="记忆" min-width="220" />
      <el-table-column prop="status" label="状态" width="100" />
      <el-table-column prop="summary" label="摘要" min-width="220" />
    </el-table>
  </div>
</template>

<style scoped>
/* 样式自 EntryEditDialog.vue 原样迁移（.edit-alert / .conflict-box），保持视觉零变化。 */
.edit-alert {
  margin-bottom: 12px;
}

.conflict-box {
  margin-top: 12px;
}

.conflict-box h4 {
  margin: 0 0 8px;
  font-size: 14px;
}
</style>
