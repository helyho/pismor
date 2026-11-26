/** 统一时间显示：保留后端时间字段的日期与时分秒，不改变时区。 */
export function formatDateTime(value?: string | null): string {
  if (!value) return '—'
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/)
  if (!match) return value
  return `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]}:${match[6]}`
}
