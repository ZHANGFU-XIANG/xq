import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { post, type Json } from '../lib/api'
import { fmtInt } from '../lib/format'

/** 從 XQ 庫存畫面複製貼上 → 解析 → 與系統比對 → 確認後同步。 */
export default function XQImport() {
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [complete, setComplete] = useState(true)
  const [res, setRes] = useState<Json | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  async function check() {
    setBusy(true)
    setMsg('')
    try {
      setRes(await post('/api/xq/import', { text, complete }))
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '解析失敗')
    } finally {
      setBusy(false)
    }
  }
  async function apply() {
    if (!res?.pending_action) return
    setBusy(true)
    try {
      const r = await post(`/api/actions/${res.pending_action}/confirm`)
      setMsg(r.message)
      setRes(null)
      setText('')
      qc.invalidateQueries()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '同步失敗')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-line bg-paper p-4">
      <ol className="mb-3 list-decimal space-y-0.5 pl-5 text-sm text-ink-2">
        <li>在 XQ 全球贏家打開「帳務」→「庫存」（證券）</li>
        <li>點表格任一格，按 Ctrl+A 全選、Ctrl+C 複製（連同表頭）</li>
        <li>貼到下面，按「比對」</li>
      </ol>
      <label htmlFor="xq-text" className="sr-only">XQ 庫存表格</label>
      <textarea id="xq-text" value={text} onChange={(e) => setText(e.target.value)} rows={5}
        placeholder={'商品代碼\t商品名稱\t類別\t庫存股數\t成本均價 …'}
        className="num w-full rounded-md border border-line bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-link" />
      <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
        <button onClick={check} disabled={busy || !text.trim()} className="rounded-md bg-ink px-4 py-1.5 text-paper disabled:opacity-40">比對</button>
        <label className="inline-flex items-center gap-1.5 text-ink-2">
          <input type="checkbox" checked={complete} onChange={(e) => setComplete(e.target.checked)} />
          這是完整庫存（系統有、XQ 沒有的視為已賣出）
        </label>
      </div>
      {msg && <p className="mt-2 text-sm">{msg}</p>}
      {res?.error && <p className="mt-2 text-sm text-up">{res.error}</p>}
      {res?.diffs && (
        <div className="mt-4">
          <table className="w-full text-[0.95rem]">
            <thead className="text-left text-sm text-muted">
              <tr className="border-b border-line"><th className="py-1 font-normal">標的</th><th className="text-right font-normal">系統股數</th><th className="text-right font-normal">XQ 股數</th><th className="pl-4 font-normal">狀態</th></tr>
            </thead>
            <tbody>
              {res.diffs.map((d: Json) => (
                <tr key={d.code} className="border-b border-line/60">
                  <td className="py-1.5"><span className="num text-muted">{d.code}</span> {d.name}</td>
                  <td className="num text-right">{fmtInt(d.system)}</td>
                  <td className="num text-right">{fmtInt(d.xq)}</td>
                  <td className={`pl-4 ${d.status === '一致' ? 'text-down' : 'text-warn'}`}>{d.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {res.in_sync ? (
            <p className="mt-3 text-down">系統紀錄與 XQ 一致，不需要調整。</p>
          ) : res.pending_action ? (
            <div className="mt-3 rounded-md border-2 border-link/50 px-3 py-2 text-sm">
              <div className="mb-2">{res.summary}</div>
              <div className="flex gap-2">
                <button onClick={apply} disabled={busy} className="rounded-md bg-link px-3 py-1 text-white disabled:opacity-50">確認同步</button>
                <button onClick={() => setRes(null)} className="rounded-md border border-line px-3 py-1">取消</button>
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}
