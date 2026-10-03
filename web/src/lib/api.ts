/* eslint-disable @typescript-eslint/no-explicit-any */
export type Json = any

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function handle(res: Response) {
  if (res.ok) return res.json()
  let msg = `伺服器錯誤（${res.status}）`
  try {
    const j = await res.json()
    if (j.detail) msg = typeof j.detail === 'string' ? j.detail : msg
    if (j.message) msg = j.message
  } catch { /* 非 JSON 錯誤 */ }
  if (res.status === 401) window.dispatchEvent(new Event('auth-required'))
  throw new ApiError(res.status, msg)
}

export const get = (path: string): Promise<Json> => fetch(path, { credentials: 'include' }).then(handle)

export const post = (path: string, body?: unknown): Promise<Json> =>
  fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(handle)

export type ChatEvent =
  | { type: 'tool'; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; name: string; ok: boolean; action: { id: string; summary: string } | null }
  | {
      type: 'answer'
      text: string
      verified: boolean
      unverified?: string[]
      sources?: { tool: string; args: Record<string, unknown>; date?: string }[]
      actions?: { id: string; summary: string }[]
    }

/** 送出對話並以 SSE 逐步接收工具進度與最終答案。 */
export async function streamChat(
  messages: { role: 'user' | 'assistant'; content: string }[],
  onEvent: (e: ChatEvent) => void,
) {
  const res = await fetch('/api/chat', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  })
  if (!res.ok || !res.body) return handle(res)
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const chunks = buf.split(/\r?\n\r?\n/)
    buf = chunks.pop() ?? ''
    for (const c of chunks) {
      const data = c
        .split(/\r?\n/)
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).trim())
        .join('')
      if (data) onEvent(JSON.parse(data))
    }
  }
}
