export interface Principal {
  subject: string
  display_name: string
  roles: string[]
  capabilities: string[]
}

export interface StatusCount {
  status: string
  count: number
}

export interface DimensionCount {
  dimension: string
  count: number
}

export interface Overview {
  users: number
  entries: number
  pending_review: number
  conflicts: number
  tombstones: number
  db_bytes: number
  wal_bytes: number
  by_status: StatusCount[]
  by_dimension: DimensionCount[]
  updated_at: string | null
}

export interface ReviewQueueItem {
  entry_id: string
  user_source: string
  user_id: string
  dimension: string
  topic: string
  summary: string
  confidence: number
  status: string
  entry_version: string
  note: string | null
}

export interface ReviewQueue {
  items: ReviewQueueItem[]
  total: number
  page: number
  page_size: number
}

export interface ReviewFacetValue {
  value: string
  count: number
}

export interface ReviewFacets {
  dimensions: ReviewFacetValue[]
  sources: ReviewFacetValue[]
}

export interface ReviewEntry {
  user_source: string
  user_id: string
  dimension: string
  topic: string
  id: string
  confidence: number
  status: string
  body: string
  summary: string
  created_at: string
  updated_at: string
  last_reinforced_at: string
  degraded_reason: string | null
  superseded_by: string | null
  topic_new: string | null
  conflict_with: string | null
  conflict_marks: string[]
  reinforcements: number
  archived_at: string | null
}

export interface ReviewSource {
  seq: number
  origin_type: string
  origin_ref: string
  quote: string
  kind: string
}

export interface ReviewHistoryItem {
  decision: string
  actor: string
  ts: string
  note: string | null
  entry_version: string
}

export interface ReviewClarification {
  conflicting_ids: string[]
  question: string
  issued_at: string
  ttl_days: number
}

export interface ReviewConflict {
  id: string
  dimension: string
  topic: string
  summary: string
  status: string
}

export interface ReviewDetail {
  entry: ReviewEntry
  sources: ReviewSource[]
  history: ReviewHistoryItem[]
  clarifications: ReviewClarification[]
  conflicts: ReviewConflict[]
  allowed_topics: string[]
}

export type ReviewDecision = 'confirm' | 'remove' | 'fix'

export interface ReviewDecisionPayload {
  user_source: string
  user_id: string
  entry_id: string
  decision: ReviewDecision
  entry_version: string
  note?: string
  body?: string
  summary?: string
  topic?: string
}

export interface ReviewDecisionResult {
  noop: boolean
  updated?: number | null
  merged_into?: string | null
  rekeyed_to?: string | null
  invalid_topic?: boolean | null
}

export type EntryStatus =
  | 'pending_review'
  | 'orphan_topic'
  | 'active'
  | 'degraded'
  | 'archived'

export interface EntryEditPayload {
  user_source: string
  user_id: string
  entry_id: string
  expected_version: string
  new_dimension: string
  new_topic: string
  confidence: number
  status: EntryStatus
  summary: string
  body: string
  degraded_reason: string
}

export interface EntryEditConflict {
  id: string
  dimension: string
  topic: string
  status: string
  summary: string
}

export interface EntryEditCheck {
  ok: boolean
  errors: string[]
  warnings: string[]
  conflicts: EntryEditConflict[]
}

export interface EntryEditResult {
  ok: boolean
  id?: string | null
  updated_at?: string | null
  warnings: string[]
}

export interface UserListItem {
  user_source: string
  user_id: string
  entries: number
  pending_review: number
  conflicts: number
  updated_at: string | null
}

export interface UserList {
  items: UserListItem[]
  total: number
  page: number
  page_size: number
}

export interface TombstoneItem {
  user_source: string
  user_id: string
  deleted_at: string
  requested_by: string
  purged_at: string | null
}

export interface TombstoneList {
  items: TombstoneItem[]
  total: number
  page: number
  page_size: number
}

export interface UserEntry {
  id: string
  dimension: string
  topic: string
  status: string
  confidence: number
  summary: string
  body: string
  created_at: string
  updated_at: string
  last_reinforced_at: string
  degraded_reason: string | null
  superseded_by: string | null
  topic_new: string | null
  conflict_with: string | null
  reinforcements: number
  archived_at: string | null
  sources_count: number
}

export interface TombstoneInfo {
  deleted_at: string
  requested_by: string
  purged_at: string | null
}

export interface UserDetail {
  user_source: string
  user_id: string
  entries: UserEntry[]
  by_dimension: DimensionCount[]
  by_status: StatusCount[]
  clarifications: ReviewClarification[]
  tombstone: TombstoneInfo | null
  entries_count: number
  pending_review: number
  conflicts: number
  first_seen: string | null
  updated_at: string | null
  last_reinforced_at: string | null
}

export interface AssembleEntry {
  id: string
  text: string
  confidence: number
  status: string
}

export interface AssembleClarification {
  conflicting_ids: string[]
  question: string
}

export interface AssembleResult {
  entries: AssembleEntry[]
  budget_used: number
  merge_task: boolean
  clarification_needed: AssembleClarification[]
  rendered: string
}

export interface EntryListItem {
  id: string
  user_source: string
  user_id: string
  dimension: string
  topic: string
  summary: string
  status: string
  confidence: number
  updated_at: string
  last_reinforced_at: string
  reinforcements: number
  conflict_with: string | null
  sources_count: number
}

export interface EntryList {
  items: EntryListItem[]
  total: number
  page: number
  page_size: number
}

export interface EntryFacetValue {
  value: string
  count: number
}

export interface EntryFacets {
  dimensions: EntryFacetValue[]
  statuses: EntryFacetValue[]
  sources: EntryFacetValue[]
}

export interface ConfigDoc {
  version: string
  path: string
  yaml: string
  data: Record<string, unknown>
  comments: Record<string, string>
  settings?: Record<string, unknown>
}

export interface ConfigChange {
  path: string
  old: unknown
  new: unknown
}

export interface ConfigImpact {
  dimension: string
  topic: string
  status: string
  count: number
  removed: boolean
  dimension_removed: boolean
}

export interface ConfigValidation {
  ok: boolean
  errors: string[]
  warnings: string[]
  changes?: ConfigChange[]
  restart_required?: string[]
  applied?: string[]
  impact?: ConfigImpact[]
}

export interface ConfigBackup {
  id: string
  name: string
  size: number
  updated_at: string
  version: string
  kind: 'settings' | 'taxonomy'
}

export interface ConfigWriteResult {
  ok: boolean
  version: string
  applied: string[]
  restart_required: string[]
  warnings: string[]
  backup: string | null
}

export interface SystemHealth {
  db_ok: boolean
  integrity: string
  error: string | null
  db_bytes: number
  wal_bytes: number
  journal_mode: string | null
  sqlite_version: string | null
  entries: number
  users: number
  pending_review: number
  tombstones: number
  updated_at: string | null
}

export interface JobRun {
  id: number
  job: string
  started_at: string
  finished_at: string | null
  ok: boolean | null
  stats: Record<string, unknown> | null
  error: string | null
}

export interface CountByName {
  [key: string]: string | number
  count: number
}

export interface SystemStats {
  by_status: Array<{ status: string; count: number }>
  by_dimension: Array<{ dimension: string; count: number }>
  by_source: Array<{ source: string; count: number }>
  top_topics: Array<{ dimension: string; topic: string; count: number }>
  review_trend: Array<{ date: string; count: number }>
  pending_backlog: Array<{ bucket: string; count: number }>
}

export interface AdminAudit {
  id: number
  actor: string
  action: string
  target: string
  ts: string
  detail: string | null
}
