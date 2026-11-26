<script setup lang="ts">
import type { ConfigValidation } from '@/api/types'

defineProps<{ result: ConfigValidation }>()
</script>

<template>
  <div class="validation-panel">
    <div v-if="result.errors.length" class="validation-block validation-errors">
      <strong>错误</strong>
      <ul>
        <li v-for="item in result.errors" :key="item">{{ item }}</li>
      </ul>
    </div>
    <div v-if="result.warnings.length" class="validation-block validation-warnings">
      <strong>警告</strong>
      <ul>
        <li v-for="item in result.warnings" :key="item">{{ item }}</li>
      </ul>
    </div>
    <div v-if="result.restart_required?.length" class="validation-block">
      <strong>需重启后生效</strong>
      <div>{{ result.restart_required.join('、') }}</div>
    </div>
    <div v-if="result.impact?.length" class="validation-block">
      <strong>存量影响</strong>
      <ul>
        <li v-for="item in result.impact" :key="`${item.dimension}-${item.topic}-${item.status}`">
          {{ item.dimension }}/{{ item.topic }} · {{ item.status }} · {{ item.count }} 条
        </li>
      </ul>
    </div>
    <div v-if="!result.errors.length && !result.warnings.length && !result.restart_required?.length && !result.impact?.length" class="validation-ok">
      校验通过
    </div>
  </div>
</template>

<style scoped>
.validation-panel {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  font-size: 13px;
}
.validation-block + .validation-block {
  margin-top: 10px;
}
.validation-block ul {
  margin: 4px 0 0;
  padding-left: 20px;
}
.validation-errors {
  color: var(--el-color-danger);
}
.validation-warnings {
  color: var(--el-color-warning);
}
.validation-ok {
  color: var(--el-color-success);
}
</style>
