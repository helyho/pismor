<script setup lang="ts">
import FieldHint from '@/components/FieldHint.vue'

defineProps<{
  label: string
  hint?: string
  readonly?: boolean
  wide?: boolean
}>()
</script>

<template>
  <div class="config-field" :class="{ 'is-readonly': readonly, 'is-wide': wide }">
    <div class="config-field__label">
      <span>{{ label }}</span>
      <FieldHint :text="hint" />
      <span v-if="readonly" class="config-field__badge">只读</span>
    </div>
    <div class="config-field__control">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.config-field {
  padding: 15px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.config-field:last-child {
  border-bottom: none;
}
.config-field__label {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 8px;
  color: var(--el-text-color-regular);
  font-size: 13px;
  font-weight: 500;
  line-height: 1.4;
  word-break: break-all;
}
.config-field__badge {
  font-size: 11px;
  font-weight: 400;
  line-height: 18px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  border-radius: 4px;
  padding: 0 6px;
}
.config-field__control {
  max-width: 560px;
  min-width: 0;
}
.config-field.is-wide .config-field__control {
  max-width: 100%;
}
.config-field.is-readonly .config-field__control {
  opacity: 1;
}
@media (max-width: 720px) {
  .config-field__control {
    max-width: 100%;
  }
}
</style>
