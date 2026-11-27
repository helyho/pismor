<script setup lang="ts">
import { ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue?: string[] | null
    placeholder?: string
  }>(),
  {
    modelValue: null,
    placeholder: '输入关键词后按回车添加',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string[]]
}>()

// 逗号、顿号、分号与换行都视为分隔符，兼容「每行一个词」的粘贴习惯。
const SEPARATORS = /[\n\r,，、;；]+/

const items = ref<string[]>([])
const draft = ref('')
const composing = ref(false)

watch(
  () => props.modelValue,
  (value) => {
    items.value = Array.isArray(value) ? [...value] : []
  },
  { immediate: true, deep: true },
)

// 运行时对英文关键词大小写不敏感（trigger.build_explicit_re 用 re.IGNORECASE），
// 去重口径与之保持一致，但保留用户输入的原始写法。
function dedupeKey(value: string): string {
  return value.toLowerCase()
}

function publish(next: string[]): void {
  items.value = next
  emit('update:modelValue', next)
}

function commit(raw?: unknown): void {
  // 模板里 @blur 会把 FocusEvent 传进来，只接受字符串作为显式文本。
  const text = typeof raw === 'string' ? raw : draft.value
  if (!text) return
  const exists = new Set(items.value.map(dedupeKey))
  const added: string[] = []
  for (const part of text.split(SEPARATORS)) {
    const item = part.trim()
    if (!item) continue
    const key = dedupeKey(item)
    if (exists.has(key)) continue
    exists.add(key)
    added.push(item)
  }
  draft.value = ''
  if (added.length) publish([...items.value, ...added])
}

function removeAt(index: number): void {
  publish(items.value.filter((_, i) => i !== index))
}

// 单行 input 粘贴多行时浏览器会把换行压成空格，这里接管粘贴，按原始文本拆分。
function onPaste(event: ClipboardEvent): void {
  const text = event.clipboardData?.getData('text/plain') ?? ''
  if (!/[\n\r,，、;；]/.test(text)) return
  event.preventDefault()
  commit(draft.value + text)
}

function onKeydown(event: Event | KeyboardEvent): void {
  if (!(event instanceof KeyboardEvent)) return
  // 中文输入法组字期间的回车只用于确认候选词，不能当成提交。
  if (event.isComposing || composing.value) return
  if (event.key === 'Enter') {
    event.preventDefault()
    commit()
  } else if (event.key === 'Backspace' && !draft.value && items.value.length) {
    removeAt(items.value.length - 1)
  }
}
</script>

<template>
  <div class="keyword-list-editor">
    <div class="keyword-list">
      <el-tag
        v-for="(item, index) in items"
        :key="`${item}-${index}`"
        class="keyword-list__tag"
        size="small"
        closable
        disable-transitions
        @close="removeAt(index)"
      >
        {{ item }}
      </el-tag>
      <el-input
        v-model="draft"
        class="keyword-list__input"
        size="small"
        :placeholder="items.length ? '' : placeholder"
        @keydown="onKeydown"
        @paste="onPaste"
        @compositionstart="composing = true"
        @compositionend="composing = false"
        @blur="commit"
      />
    </div>
    <p v-if="!items.length" class="keyword-list__hint">
      至少保留一个关键词；为空时保存会被拒绝。
    </p>
  </div>
</template>

<style scoped>
.keyword-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  background: var(--el-fill-color-blank);
  transition: border-color 0.2s;
}
.keyword-list:focus-within {
  border-color: var(--el-color-primary);
}
.keyword-list__tag {
  max-width: 100%;
}
.keyword-list__input {
  flex: 1 1 180px;
  min-width: 140px;
}
.keyword-list__input :deep(.el-input__wrapper) {
  padding: 0 4px;
  background: transparent;
  box-shadow: none;
}
.keyword-list__hint {
  margin: 6px 0 0;
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.5;
}
</style>
