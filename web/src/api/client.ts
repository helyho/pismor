import type {
  EntryCreatePayload,
  EntryCreateResult,
  EntryValidateResult,
  TopicMap,
} from './types'

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

const BASE = '/admin/api'

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (!headers.has('Content-Type') && init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${BASE}${path}`, { ...init, headers })
  if (!response.ok) {
    let detail: unknown = response.statusText
    const text = await response.text()
    if (text) {
      try {
        const parsed = JSON.parse(text)
        detail = parsed.detail ?? parsed
      } catch {
        detail = text
      }
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function validateCreateEntry(
  payload: EntryCreatePayload,
): Promise<EntryValidateResult> {
  return api('/entries/validate-create', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function createEntry(payload: EntryCreatePayload): Promise<EntryCreateResult> {
  return api('/entries', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function listEntryTopics(): Promise<TopicMap> {
  return api('/entries/topics')
}
