<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api, ApiError } from '@/api/client'
import type {
  ConfigBackup,
  ConfigDoc,
  ConfigValidation,
  ConfigWriteResult,
} from '@/api/types'
import ConfigField from '@/components/ConfigField.vue'
import KeywordListEditor from '@/components/KeywordListEditor.vue'
import TopicAliasesEditor from '@/components/TopicAliasesEditor.vue'
import TopicOverridesEditor from '@/components/TopicOverridesEditor.vue'
import ValidationPanel from '@/components/ValidationPanel.vue'
import { formatDateTime } from '@/utils/format'

type AnyRecord = Record<string, any>

const activeTab = ref<'settings' | 'taxonomy' | 'history'>('settings')
const settingsDoc = ref<ConfigDoc | null>(null)
const taxonomyDoc = ref<ConfigDoc | null>(null)
const settingsDraft = ref<AnyRecord>({})
const taxonomyDraft = ref<AnyRecord>({})
const settingsYaml = ref('')
const taxonomyYaml = ref('')
const settingsMode = ref<'form' | 'yaml'>('form')
const taxonomyMode = ref<'form' | 'yaml'>('form')
const validation = ref<ConfigValidation | null>(null)
const message = ref('')
const error = ref('')
const loading = ref(false)
const saving = ref(false)
const backups = ref<ConfigBackup[]>([])

const sections = ['llm', 'embedding', 'gate', 'pipeline', 'aggregator', 'assembler', 'intake', 'backup', 'serve']
const sectionLabels: Record<string, string> = {
  llm: 'LLM 连接',
  embedding: '向量嵌入',
  gate: '置信门控',
  pipeline: '抽取管线',
  aggregator: '日批聚合',
  assembler: '记忆装配',
  intake: '事件接入',
  backup: '备份',
  serve: '服务',
}
const readonlyPaths = new Set(['db_path', 'taxonomy_path', 'embedding.dimensions'])

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function sectionData(name: string): AnyRecord {
  const section = settingsDraft.value[name]
  return section && typeof section === 'object' && !Array.isArray(section) ? section : {}
}

function fieldValue(section: string, key: string): any {
  return sectionData(section)[key]
}

function setField(section: string, key: string, value: any): void {
  sectionData(section)[key] = value
}

function hint(path: string): string | undefined {
  return settingsDoc.value?.comments[path]
}

function taxonomyHint(path: string): string | undefined {
  return taxonomyDoc.value?.comments[path]
}

function isReadonly(path: string): boolean {
  return readonlyPaths.has(path)
}

function firstEmptyListField(): string | null {
  for (const section of sections) {
    const data = sectionData(section)
    for (const [key, value] of Object.entries(data)) {
      if (Array.isArray(value) && value.length === 0) return `${section}.${key}`
    }
  }
  return null
}

function errorText(value: unknown): string {
  if (value instanceof ApiError) {
    const detail = value.detail as any
    if (detail?.errors && Array.isArray(detail.errors)) return detail.errors.join('\n')
    if (typeof detail === 'string') return detail
    if (detail?.message) return String(detail.message)
    return JSON.stringify(detail)
  }
  return value instanceof Error ? value.message : String(value)
}

async function loadSettings(): Promise<void> {
  try {
    const doc = await api<ConfigDoc>('/config/settings')
    settingsDoc.value = doc
    settingsDraft.value = clone(doc.data) as AnyRecord
    settingsYaml.value = doc.yaml
  } catch (e) {
    error.value = errorText(e)
  }
}

async function loadTaxonomy(): Promise<void> {
  try {
    const doc = await api<ConfigDoc>('/config/taxonomy')
    taxonomyDoc.value = doc
    taxonomyDraft.value = clone(doc.data) as AnyRecord
    taxonomyYaml.value = doc.yaml
  } catch (e) {
    error.value = errorText(e)
  }
}

async function loadBackups(): Promise<void> {
  try {
    const [settings, taxonomy] = await Promise.all([
      api<ConfigBackup[]>('/config/settings/backups'),
      api<ConfigBackup[]>('/config/taxonomy/backups'),
    ])
    backups.value = [...settings, ...taxonomy].sort((a, b) =>
      a.updated_at < b.updated_at ? 1 : -1,
    )
  } catch {
    // 历史版本为空或目录不存在时不影响编辑。
  }
}

async function refreshAll(): Promise<void> {
  loading.value = true
  error.value = ''
  message.value = ''
  try {
    await Promise.all([loadSettings(), loadTaxonomy(), loadBackups()])
  } finally {
    loading.value = false
  }
}

function settingsPayload(): { yaml?: string; data?: AnyRecord } {
  return settingsMode.value === 'yaml' ? { yaml: settingsYaml.value } : { data: settingsDraft.value }
}

function taxonomyPayload(): { yaml?: string; data?: AnyRecord } {
  return taxonomyMode.value === 'yaml' ? { yaml: taxonomyYaml.value } : { data: taxonomyDraft.value }
}

async function validateSettings(): Promise<void> {
  error.value = ''
  message.value = ''
  try {
    validation.value = await api<ConfigValidation>('/config/settings/validate', {
      method: 'POST',
      body: JSON.stringify(settingsPayload()),
    })
  } catch (e) {
    error.value = errorText(e)
  }
}

async function validateTaxonomy(): Promise<void> {
  error.value = ''
  message.value = ''
  try {
    validation.value = await api<ConfigValidation>('/config/taxonomy/validate', {
      method: 'POST',
      body: JSON.stringify(taxonomyPayload()),
    })
  } catch (e) {
    error.value = errorText(e)
  }
}

async function saveSettings(): Promise<void> {
  if (!settingsDoc.value) return
  if (settingsMode.value === 'form') {
    const empty = firstEmptyListField()
    if (empty) {
      error.value = `${empty} 至少保留一个取值`
      return
    }
  }
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await api<ConfigWriteResult>('/config/settings', {
      method: 'PUT',
      body: JSON.stringify({
        expected_version: settingsDoc.value.version,
        ...settingsPayload(),
      }),
    })
    message.value = `已保存 v${result.version.slice(0, 12)}`
    await Promise.all([loadSettings(), loadBackups()])
  } catch (e) {
    error.value = errorText(e)
  } finally {
    saving.value = false
  }
}

async function saveTaxonomy(): Promise<void> {
  if (!taxonomyDoc.value) return
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await api<ConfigWriteResult>('/config/taxonomy', {
      method: 'PUT',
      body: JSON.stringify({
        expected_version: taxonomyDoc.value.version,
        ...taxonomyPayload(),
      }),
    })
    message.value = `维度已保存并热更新 v${result.version.slice(0, 12)}`
    await Promise.all([loadTaxonomy(), loadBackups()])
  } catch (e) {
    error.value = errorText(e)
  } finally {
    saving.value = false
  }
}

async function restore(row: unknown): Promise<void> {
  const backup = row as ConfigBackup
  try {
    await ElMessageBox.confirm(
      `用备份「${backup.name}」覆盖当前配置？当前文件会先自动备份。`,
      '恢复历史版本',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api(`/config/${backup.kind}/backups/${encodeURIComponent(backup.id)}/restore`, {
      method: 'POST',
    })
    ElMessage.success('已恢复历史版本')
    validation.value = null
    await refreshAll()
  } catch (e) {
    error.value = errorText(e)
  }
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function dimensionField(dim: AnyRecord, key: string): any {
  return dim[key]
}

function setTopics(dim: AnyRecord, topics: string[]): void {
  dim.topics = topics
  const disabled = Array.isArray(dim.disabled_topics) ? dim.disabled_topics : []
  dim.disabled_topics = disabled.filter((topic: string) => topics.includes(topic))
}

function setDisabledTopics(dim: AnyRecord, topics: string[]): void {
  const allowed = new Set(Array.isArray(dim.topics) ? dim.topics : [])
  dim.disabled_topics = topics.filter((topic) => allowed.has(topic))
}

function addDimension(): void {
  if (!Array.isArray(taxonomyDraft.value.dimensions)) {
    taxonomyDraft.value.dimensions = []
  }
  const list = taxonomyDraft.value.dimensions as AnyRecord[]
  const used = new Set(list.map((dim) => String(dim.key ?? '')))
  let key = 'new_dimension'
  let suffix = 2
  while (used.has(key)) {
    key = `new_dimension_${suffix}`
    suffix += 1
  }
  const priorities = list.map((dim) => Number(dim.priority) || 0)
  list.push({
    key,
    name: '新维度',
    priority: Math.max(0, ...priorities) + 10,
    topics: [],
    guidance: '',
    sensitive: false,
    topic_aliases: {},
    topic_overrides: {},
    disabled_topics: [],
  })
  taxonomyMode.value = 'form'
  activeDimension.value = String(list.length - 1)
  message.value = '已新增维度，请填写后校验并保存'
}

const dimensions = computed<AnyRecord[]>(() =>
  Array.isArray(taxonomyDraft.value.dimensions) ? taxonomyDraft.value.dimensions : [],
)

const activeDimension = ref('0')

function dimensionLabel(dim: AnyRecord, index: number): string {
  return String(dim.name || dim.key || `维度 ${index + 1}`)
}

const dimensionIssues = computed<Record<number, 'error' | 'warning'>>(() => {
  const issues: Record<number, 'error' | 'warning'> = {}
  const result = validation.value
  if (!result) return issues
  const mark = (messages: string[], level: 'error' | 'warning'): void => {
    for (const message of messages) {
      const match = /^dimensions\[(\d+)\]/.exec(message)
      if (!match) continue
      const index = Number(match[1])
      if (level === 'error' || !issues[index]) issues[index] = level
    }
  }
  mark(result.warnings, 'warning')
  mark(result.errors, 'error')
  return issues
})

function dimensionIssue(index: number): 'error' | 'warning' | null {
  return dimensionIssues.value[index] ?? null
}

watch(
  () => dimensions.value.length,
  (length) => {
    const index = Number(activeDimension.value)
    if (!Number.isInteger(index) || index < 0 || index >= length) {
      activeDimension.value = length ? '0' : ''
    }
  },
)

function onTabChange(name: string | number): void {
  if (name === 'history') void loadBackups()
}

onMounted(refreshAll)
</script>

<template>
  <div class="config-view">
    <div class="config-head">
      <div>
        <h2>配置中心</h2>
        <p class="muted">维度保存后立即生效；运行配置中连接类字段保存后提示重启。</p>
      </div>
      <el-button :loading="loading" @click="refreshAll">刷新</el-button>
    </div>

    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" class="config-alert" />
    <el-alert v-if="message" type="success" :title="message" show-icon :closable="false" class="config-alert" />

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane label="运行配置" name="settings">
        <div class="config-toolbar">
          <el-radio-group v-model="settingsMode" size="small">
            <el-radio-button value="form">结构化</el-radio-button>
            <el-radio-button value="yaml">YAML 源码</el-radio-button>
          </el-radio-group>
          <div class="config-actions">
            <el-button size="small" @click="validateSettings">校验</el-button>
            <el-button size="small" type="primary" :loading="saving" @click="saveSettings">保存</el-button>
          </div>
        </div>

        <template v-if="settingsMode === 'form'">
          <el-card shadow="never" class="config-card">
            <template #header>
              <div class="config-card__head">
                <strong>基础路径</strong>
                <span class="section-name">paths</span>
              </div>
            </template>
            <ConfigField label="db_path" :hint="hint('db_path')" readonly>
              <span class="config-value">{{ settingsDraft.db_path }}</span>
            </ConfigField>
            <ConfigField label="taxonomy_path" :hint="hint('taxonomy_path')" readonly>
              <span class="config-value">{{ settingsDraft.taxonomy_path }}</span>
            </ConfigField>
          </el-card>

          <el-card
            v-for="section in sections"
            :key="section"
            shadow="never"
            class="config-card"
          >
            <template #header>
              <div class="config-card__head">
                <strong>{{ sectionLabels[section] ?? section }}</strong>
                <span class="section-name">{{ section }}</span>
              </div>
            </template>
            <ConfigField
              v-for="key in Object.keys(sectionData(section))"
              :key="`${section}.${key}`"
              :label="key"
              :hint="hint(`${section}.${key}`)"
              :readonly="isReadonly(`${section}.${key}`)"
              :wide="Array.isArray(fieldValue(section, key))"
            >
              <span
                v-if="isReadonly(`${section}.${key}`)"
                class="config-value"
                :class="{ 'config-value--tags': Array.isArray(fieldValue(section, key)) }"
              >
                <template v-if="Array.isArray(fieldValue(section, key))">
                  <el-tag
                    v-for="item in fieldValue(section, key)"
                    :key="item"
                    size="small"
                    type="info"
                    disable-transitions
                  >
                    {{ item }}
                  </el-tag>
                </template>
                <template v-else>{{ fieldValue(section, key) }}</template>
              </span>
              <el-switch
                v-else-if="typeof fieldValue(section, key) === 'boolean'"
                :model-value="fieldValue(section, key)"
                @update:model-value="(value) => setField(section, key, value)"
              />
              <el-input-number
                v-else-if="typeof fieldValue(section, key) === 'number'"
                :model-value="fieldValue(section, key)"
                :step="Number.isInteger(fieldValue(section, key)) ? 1 : 0.01"
                controls-position="right"
                @update:model-value="(value) => setField(section, key, value)"
              />
              <KeywordListEditor
                v-else-if="Array.isArray(fieldValue(section, key))"
                :model-value="fieldValue(section, key)"
                @update:model-value="(items) => setField(section, key, items)"
              />
              <el-input
                v-else
                :model-value="fieldValue(section, key) ?? ''"
                @update:model-value="(value) => setField(section, key, value)"
              />
            </ConfigField>
          </el-card>
        </template>

        <el-input
          v-else
          v-model="settingsYaml"
          type="textarea"
          :rows="26"
          class="yaml-editor"
          spellcheck="false"
        />

        <ValidationPanel v-if="validation" :result="validation" />
      </el-tab-pane>

      <el-tab-pane label="维度" name="taxonomy">
        <div class="config-toolbar">
          <el-radio-group v-model="taxonomyMode" size="small">
            <el-radio-button value="form">结构化</el-radio-button>
            <el-radio-button value="yaml">YAML 源码</el-radio-button>
          </el-radio-group>
          <div class="config-actions">
            <el-button size="small" plain @click="addDimension">新增维度</el-button>
            <el-button size="small" @click="validateTaxonomy">校验</el-button>
            <el-button size="small" type="primary" :loading="saving" @click="saveTaxonomy">保存</el-button>
          </div>
        </div>

        <template v-if="taxonomyMode === 'form'">
          <el-card shadow="never" class="config-card">
            <template #header>元信息</template>
            <ConfigField label="meta.version" :hint="taxonomyHint('meta.version')">
              <el-input
                :model-value="taxonomyDraft.meta?.version ?? ''"
                @update:model-value="(value) => (taxonomyDraft.meta.version = value)"
              />
            </ConfigField>
            <ConfigField label="meta.auto_activate" :hint="taxonomyHint('meta.auto_activate')">
              <el-switch
                :model-value="Boolean(taxonomyDraft.meta?.auto_activate)"
                @update:model-value="(value) => (taxonomyDraft.meta.auto_activate = value)"
              />
            </ConfigField>
          </el-card>

          <el-tabs v-if="dimensions.length" v-model="activeDimension" class="dimension-tabs">
            <el-tab-pane
              v-for="(dim, index) in dimensions"
              :key="`${dim.key}-${index}`"
              :name="String(index)"
            >
              <template #label>
                <span class="dimension-tab-label">
                  <span class="dimension-tab-text">{{ dimensionLabel(dim, index) }}</span>
                  <span
                    v-if="dimensionIssue(index)"
                    class="dimension-tab-dot"
                    :class="`is-${dimensionIssue(index)}`"
                  />
                </span>
              </template>
              <el-card shadow="never" class="config-card">
                <template #header>
                  <div class="dimension-head">
                    <span class="section-name">{{ dim.key || '未设置 key' }}</span>
                    <span v-if="dimensionIssue(index) === 'error'" class="dimension-flag is-error">
                      本维度有校验错误
                    </span>
                    <span
                      v-else-if="dimensionIssue(index) === 'warning'"
                      class="dimension-flag is-warning"
                    >
                      本维度有提示
                    </span>
                  </div>
                </template>
                <ConfigField label="key" :hint="taxonomyHint('dimensions[].key')">
                  <el-input
                    :model-value="dimensionField(dim, 'key')"
                    @update:model-value="(value) => (dim.key = value)"
                  />
                </ConfigField>
                <ConfigField label="name" :hint="taxonomyHint('dimensions[].name')">
                  <el-input
                    :model-value="dimensionField(dim, 'name')"
                    @update:model-value="(value) => (dim.name = value)"
                  />
                </ConfigField>
                <ConfigField label="priority" :hint="taxonomyHint('dimensions[].priority')">
                  <el-input-number
                    :model-value="dimensionField(dim, 'priority')"
                    :step="1"
                    controls-position="right"
                    @update:model-value="(value) => (dim.priority = value)"
                  />
                </ConfigField>
                <ConfigField label="sensitive" :hint="taxonomyHint('dimensions[].sensitive')">
                  <el-select
                    :model-value="String(dimensionField(dim, 'sensitive') ?? 'false')"
                    style="width: 100%"
                    @update:model-value="(value) => (dim.sensitive = value)"
                  >
                    <el-option label="false" value="false" />
                    <el-option label="partial" value="partial" />
                    <el-option label="true" value="true" />
                  </el-select>
                </ConfigField>
                <ConfigField label="guidance" :hint="taxonomyHint('dimensions[].guidance')" wide>
                  <el-input
                    type="textarea"
                    :rows="5"
                    :model-value="dimensionField(dim, 'guidance')"
                    @update:model-value="(value) => (dim.guidance = value)"
                  />
                </ConfigField>
                <ConfigField label="topics" :hint="taxonomyHint('dimensions[].topics')">
                  <el-select
                    :model-value="dimensionField(dim, 'topics') ?? []"
                    multiple
                    filterable
                    allow-create
                    default-first-option
                    style="width: 100%"
                    @update:model-value="(value) => setTopics(dim, value)"
                  >
                    <el-option v-for="topic in dim.topics ?? []" :key="topic" :label="topic" :value="topic" />
                  </el-select>
                </ConfigField>
                <ConfigField
                  label="disabled_topics"
                  :hint="taxonomyHint('dimensions[].disabled_topics')"
                >
                  <el-select
                    :model-value="dim.disabled_topics ?? []"
                    multiple
                    filterable
                    style="width: 100%"
                    @update:model-value="(value) => setDisabledTopics(dim, value)"
                  >
                    <el-option v-for="topic in dim.topics ?? []" :key="topic" :label="topic" :value="topic" />
                  </el-select>
                  <div class="muted">停用后保留存量记忆，只阻止新记忆；存量记忆不会被审计隔离。</div>
                </ConfigField>
                <ConfigField label="topic_aliases" :hint="taxonomyHint('dimensions[].topic_aliases')" wide>
                  <TopicAliasesEditor
                    :model-value="dim.topic_aliases"
                    :topics="dim.topics ?? []"
                    @update:model-value="(value) => (dim.topic_aliases = value)"
                  />
                </ConfigField>
                <ConfigField
                  label="topic_overrides"
                  :hint="taxonomyHint('dimensions[].topic_overrides')"
                  wide
                >
                  <TopicOverridesEditor
                    :model-value="dim.topic_overrides"
                    :topics="dim.topics ?? []"
                    @update:model-value="(value) => (dim.topic_overrides = value)"
                  />
                </ConfigField>
              </el-card>
            </el-tab-pane>
          </el-tabs>
          <el-empty v-else description="还没有维度，点击「新增维度」开始" />
        </template>

        <el-input
          v-else
          v-model="taxonomyYaml"
          type="textarea"
          :rows="30"
          class="yaml-editor"
          spellcheck="false"
        />

        <ValidationPanel v-if="validation" :result="validation" />
      </el-tab-pane>

      <el-tab-pane label="历史版本" name="history">
        <el-table :data="backups" stripe>
          <el-table-column prop="kind" label="类型" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.kind === 'taxonomy' ? 'warning' : 'info'">
                {{ row.kind === 'taxonomy' ? '维度' : '配置' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="备份文件" min-width="300" show-overflow-tooltip />
          <el-table-column prop="version" label="版本" width="130" />
          <el-table-column label="时间" width="190">
            <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="大小" width="100">
            <template #default="{ row }">{{ formatBytes(row.size) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="100" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="restore(row)">恢复</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <span class="muted">尚无备份；保存一次配置后生成。</span>
          </template>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.config-view {
  max-width: 1120px;
}
.config-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}
.config-head h2 {
  margin: 0 0 4px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.config-alert {
  margin-bottom: 10px;
}
.config-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.config-actions {
  display: flex;
  gap: 8px;
}
.config-card {
  margin-bottom: 14px;
}
.config-card :deep(.el-card__header) {
  padding: 16px 20px;
}
.config-card :deep(.el-card__body) {
  padding: 4px 20px 10px;
}
.config-card__head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.config-value {
  display: block;
  color: var(--el-text-color-regular);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-all;
}
.config-value--tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.config-value--tags :deep(.el-tag) {
  max-width: 100%;
}
.section-name {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-family: var(--el-font-family-mono, monospace);
}
.yaml-editor :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
}
.dimension-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.dimension-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}
.dimension-tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
}
.dimension-tab-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dimension-tab-dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-color-warning);
}
.dimension-tab-dot.is-error {
  background: var(--el-color-danger);
}
.dimension-flag {
  margin-left: 4px;
  font-size: 12px;
}
.dimension-flag.is-error {
  color: var(--el-color-danger);
}
.dimension-flag.is-warning {
  color: var(--el-color-warning);
}
</style>
