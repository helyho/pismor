<script setup lang="ts">
const props = defineProps<{ text?: string }>()
</script>

<template>
  <el-tooltip v-if="props.text" :content="props.text" placement="top" :show-after="120">
    <button type="button" class="field-hint" :aria-label="`参数说明：${props.text}`">?</button>
  </el-tooltip>
</template>

<style scoped>
.field-hint {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1px solid var(--el-border-color);
  background: transparent;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1;
  cursor: help;
  padding: 0;
}
.field-hint:hover,
.field-hint:focus-visible {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary);
  outline: none;
}
</style>
