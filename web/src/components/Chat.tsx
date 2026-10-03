import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { post, streamChat, type ChatEvent } from '../lib/api'

type Action = { id: string; summary: string; state?: 'done' | 'cancelled' | 'error'; message?: string }
type Msg = {
  role: 'user' | 'assistant'
  content: string
  progress?: string[]
  verified?: boolean
  unverified?: string[]
  sources?: { tool: string; args: Record<string, unknown>; date?: string }[]
  actions?: Action[]
}

const TOOL_NAMES: Record<string, string> = {
  find_stock: '搜尋股票', stock_profile: '查詢基本盤', analyze_stock: '分析個股', get_daily_report: '讀取每日報告',
  get_portfolio: '讀取持股', search_news: '搜尋新聞', get_macro: '讀取國際大事', get_rules: '讀取競賽規則',
  get_model_health: '讀取模型表現', simulate_trades: '模擬試算', record_trade: '準備記錄成交', run_analysis: '準備重跑分析',
}

const EXAMPLES = ['今天建議買哪些？為什麼？', '2603 長榮現在可以買嗎？要買幾張？', '如果我把聯電換成台積電，持股率會變多少？', '我剛買了 2892 第一金 10 張 38.5 元']

type Ctx = { open: boolean; setOpen: (v: boolean) => void; ask: (q: string) => void; pending: string | null; clearPending: () => void }
const ChatCtx = createContext<Ctx | null>(null)

export function ChatProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState<string | null>(null)
  return (
    <ChatCtx.Provider value={{ open, setOpen, pending, clearPending: () => setPending(null), ask: (q) => { setPending(q); setOpen(true) } }}>
      {children}
    </ChatCtx.Provider>
  )
}

export const useChat = () => {
  const c = useContext(ChatCtx)
  if (!c) throw new Error('ChatProvider missing')
  return c
}

function escape(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** 極簡 Markdown：粗體、條列、段落；標記〔未核實〕數字。 */
function renderMd(text: string) {
  const lines = escape(text).split('\n')
  let html = ''
  let list: 'ul' | 'ol' | null = null
  const inline = (s: string) =>
    s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/([\d,.+-]+%?)〔未核實〕/g, '<span class="unverified" title="此數字在工具資料中找不到">$1</span>')
  let table: string[][] = []
  const flushTable = () => {
    if (!table.length) return
    const [head, ...body] = table
    html += '<div class="md-table"><table><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>'
    html += body.map((r) => '<tr>' + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>').join('')
    html += '</tbody></table></div>'
    table = []
  }
  for (const raw of lines) {
    const l = raw.trim()
    if (l.startsWith('|') && l.endsWith('|')) {
      if (list) {
        html += `</${list}>`
        list = null
      }
      const cells = l.slice(1, -1).split('|').map((c) => c.trim())
      if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) table.push(cells)
      continue
    }
    flushTable()
    const ul = l.match(/^[*-]\s+(.*)$/)
    const ol = l.match(/^\d+[.、]\s*(.*)$/)
    if (ul || ol) {
      const kind = ul ? 'ul' : 'ol'
      if (list !== kind) {
        if (list) html += `</${list}>`
        html += `<${kind}>`
        list = kind
      }
      html += `<li>${inline((ul || ol)![1])}</li>`
      continue
    }
    if (list) {
      html += `</${list}>`
      list = null
    }
    if (l) html += `<p>${inline(l)}</p>`
  }
  if (list) html += `</${list}>`
  flushTable()
  return { __html: html }
}

export default function ChatPanel({ compact = false }: { compact?: boolean }) {
  const { pending, clearPending } = useChat()
  const qc = useQueryClient()
  const [msgs, setMsgs] = useState<Msg[]>(() => {
    try {
      return JSON.parse(sessionStorage.getItem('chat') || '[]')
    } catch {
      return []
    }
  })
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const end = useRef<HTMLDivElement>(null)

  useEffect(() => {
    try {
      sessionStorage.setItem('chat', JSON.stringify(msgs.slice(-30)))
    } catch { /* 儲存失敗不影響使用 */ }
    end.current?.scrollIntoView({ block: 'end' })
  }, [msgs])

  async function send(text: string) {
    const q = text.trim()
    if (!q || busy) return
    setInput('')
    setBusy(true)
    const history = [...msgs, { role: 'user' as const, content: q }]
    setMsgs([...history, { role: 'assistant', content: '', progress: [] }])
    const patch = (fn: (m: Msg) => Msg) => setMsgs((cur) => [...cur.slice(0, -1), fn(cur[cur.length - 1])])
    try {
      await streamChat(
        history.map((m) => ({ role: m.role, content: m.content })),
        (e: ChatEvent) => {
          if (e.type === 'tool') patch((m) => ({ ...m, progress: [...(m.progress ?? []), `${TOOL_NAMES[e.name] ?? e.name}${e.args.code ? ` ${e.args.code}` : e.args.query ? `「${e.args.query}」` : ''}`] }))
          if (e.type === 'answer')
            patch((m) => ({ ...m, content: e.text, verified: e.verified, unverified: e.unverified, sources: e.sources, actions: e.actions }))
        },
      )
    } catch (err) {
      patch((m) => ({ ...m, content: err instanceof Error ? err.message : '發生錯誤', verified: false }))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (pending && !busy) {
      send(pending)
      clearPending()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending])

  async function act(mi: number, a: Action, kind: 'confirm' | 'cancel') {
    const setA = (upd: Partial<Action>) =>
      setMsgs((cur) => cur.map((m, i) => (i !== mi ? m : { ...m, actions: m.actions?.map((x) => (x.id === a.id ? { ...x, ...upd } : x)) })))
    try {
      const r = await post(`/api/actions/${a.id}/${kind}`)
      setA({ state: kind === 'confirm' ? (r.ok ? 'done' : 'error') : 'cancelled', message: r.message })
      if (kind === 'confirm') qc.invalidateQueries()
    } catch (err) {
      setA({ state: 'error', message: err instanceof Error ? err.message : '執行失敗' })
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className={`min-h-0 flex-1 overflow-y-auto ${compact ? 'px-3' : 'px-1'} py-3`} aria-live="polite">
        {!msgs.length && (
          <div className="text-sm text-ink-2">
            <p className="mb-3">問任何一檔股票、今天的建議、持股或競賽規則。回答裡的數字都來自系統計算，並會逐一核對。</p>
            <div className="flex flex-col gap-2">
              {EXAMPLES.map((q) => (
                <button key={q} onClick={() => send(q)} className="rounded-md border border-line bg-paper px-3 py-2 text-left hover:border-link">
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((m, i) =>
          m.role === 'user' ? (
            <div key={i} className="mb-3 ml-8 rounded-lg bg-ink px-3 py-2 text-paper">{m.content}</div>
          ) : (
            <div key={i} className="mb-4 mr-2">
              {!!m.progress?.length && (
                <div className="mb-1 flex flex-wrap gap-1 text-xs text-muted">
                  {m.progress.map((p, j) => (
                    <span key={j} className="rounded bg-mist px-1.5 py-0.5">{p}</span>
                  ))}
                </div>
              )}
              {m.content ? (
                <div className="prose-answer rounded-lg border border-line bg-paper px-3 py-2 text-[0.95rem] leading-relaxed" dangerouslySetInnerHTML={renderMd(m.content)} />
              ) : (
                <div className="flex items-center gap-2 px-1 text-sm text-muted"><span className="h-2 w-2 animate-pulse rounded-full bg-muted" />分析中</div>
              )}
              {m.actions?.map((a) => (
                <div key={a.id} className="mt-2 rounded-lg border-2 border-link/50 bg-paper px-3 py-2 text-sm">
                  <div className="font-medium">{a.summary}</div>
                  {a.state ? (
                    <div className={`mt-1 ${a.state === 'error' ? 'text-up' : 'text-ink-2'}`}>{a.message ?? (a.state === 'cancelled' ? '已取消' : '')}</div>
                  ) : (
                    <div className="mt-2 flex gap-2">
                      <button onClick={() => act(i, a, 'confirm')} className="rounded-md bg-link px-3 py-1 text-white">確認執行</button>
                      <button onClick={() => act(i, a, 'cancel')} className="rounded-md border border-line px-3 py-1">取消</button>
                    </div>
                  )}
                </div>
              ))}
              {m.content && (
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                  {m.verified ? <span className="text-down">數字已核對</span> : m.unverified?.length ? <span className="text-warn">有 {m.unverified.length} 個數字無法核對（虛線標示）</span> : null}
                  {m.sources?.length ? <span>資料：{[...new Set(m.sources.map((s) => TOOL_NAMES[s.tool] ?? s.tool))].join('、')}{m.sources.find((s) => s.date)?.date ? `（${m.sources.find((s) => s.date)?.date}）` : ''}</span> : null}
                </div>
              )}
            </div>
          ),
        )}
        <div ref={end} />
      </div>
      <form
        className={`flex gap-2 border-t border-line ${compact ? 'p-3' : 'pt-3'}`}
        onSubmit={(e) => {
          e.preventDefault()
          send(input)
        }}
      >
        <label htmlFor="chat-input" className="sr-only">輸入問題</label>
        <textarea
          id="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              send(input)
            }
          }}
          rows={2}
          placeholder="例如：南亞還能抱嗎？"
          className="min-h-0 flex-1 resize-none rounded-md border border-line bg-paper px-3 py-2 text-[0.95rem] text-ink outline-none focus:border-link"
        />
        <div className="flex flex-col gap-1">
          <button type="submit" disabled={busy || !input.trim()} className="rounded-md bg-ink px-4 py-2 text-paper disabled:opacity-40">送出</button>
          {!!msgs.length && (
            <button type="button" onClick={() => setMsgs([])} className="text-xs text-muted hover:text-ink">清除對話</button>
          )}
        </div>
      </form>
    </div>
  )
}
