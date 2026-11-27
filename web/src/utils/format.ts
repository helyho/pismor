import { ApiError } from '@/api/client'
import type { EntryWriteErrorDetail, FieldError } from '@/api/types'

/** 统一时间显示：保留后端时间字段的日期与时分秒，不改变时区。 */
export function formatDateTime(value?: string | null): string {
  if (!value) return '—'
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/)
  if (!match) return value
  return `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]}:${match[6]}`
}

/** 单条校验错误 → 展示文案：字符串原样返回，FieldError 归一为 "field: message"。 */
export function formatErrorItem(item: FieldError | string): string {
  return typeof item === 'string' ? item : `${item.field}: ${item.message}`
}

/** ApiError / Error → 面向用户的文案（自 EntryEditDialog.vue 原样迁出，行为不变）。 */
export function formatError(value: unknown): string {
  if (value instanceof ApiError) {
    const detail = value.detail as EntryWriteErrorDetail | string | null
    if (typeof detail === 'string') return detail
    if (detail?.errors && Array.isArray(detail.errors)) {
      return detail.errors.map(formatErrorItem).join('\n')
    }
    if (detail?.message) return String(detail.message)
    return JSON.stringify(detail)
  }
  return value instanceof Error ? value.message : String(value)
}
