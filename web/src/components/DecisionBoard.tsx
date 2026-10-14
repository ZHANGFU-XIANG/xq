import { useQuery } from '@tanstack/react-query'
import { Fragment, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { get, type Json } from '../lib/api'
import { fmtPct, tone } from '../lib/format'
import { liveEvery, useQuotes } from '../lib/live'
import { useChat } from './Chat'
import { Change, Tabs } from './ui'

export const VERDICT_STYLE: Record<string, string> = {
  可以買: 'bg-up text-white',
  等回檔再買: 'bg-warn/15 text-warn ring-1 ring-warn/40',
  先不要買: 'bg-mist text-muted ring-1 ring-line',
  續抱: 'bg-link/10 text-link ring-1 ring-link/30',
  減碼: 'bg-warn text-white',
  全部出清: 'bg-down text-white',
  加碼: 'bg-up text-white',
}

type TabKey = 'hold' | 'buy' | 'wait' | 'avoid'

/** 結論的重點：去掉開頭重複的結論字樣，只留「做什麼」。 */
const gist = (x: Json) => String(x.headline ?? '').replace(/^[^：]*：/, '')

function Row({ x, q, open, onToggle }: { x: Json; q?: Json; open: boolean; onToggle: () => void }) {
  const { ask } = useChat()
  const price = q?.price ?? x.price
  return (
    <Fragment>
      <tr onClick={onToggle} className={`cursor-pointer align-top hover:bg-mist/60 md:border-b md:border-line/70 ${open ? 'bg-mist/60' : ''}`}>
        <td className="py-2.5 pl-4 pr-2">
          <span className={`inline-block whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-bold ${VERDICT_STYLE[x.verdict] ?? ''}`}>
            {x.swap_tip ? '續抱｜留意換股' : x.verdict}
          </span>
        </td>
        <td className="whitespace-nowrap py-2.5 pr-3">
          <Link to={`/stock/${x.code}`} onClick={(e) => e.stopPropagation()} className="font-bold text-ink no-underline hover:text-link">{x.name}</Link>
          <div className="num text-xs text-muted">{x.code}</div>
        </td>
        <td className={`num whitespace-nowrap py-2.5 pr-3 text-right font-medium ${tone(q?.chg_pct)}`}>{price ?? '—'}</td>
        <td className="whitespace-nowrap py-2.5 pr-3 text-right"><Change value={q?.chg_pct} /></td>
        <td className={`num hidden whitespace-nowrap py-2.5 pr-3 text-right sm:table-cell ${tone(x.pnl_pct)}`}>
          {x.pnl_pct != null ? fmtPct(x.pnl_pct) : x.limit_price != null ? <span className="text-ink-2">限 {x.limit_price}</span> : '—'}
        </td>
        <td className="hidden py-2.5 pr-4 text-sm leading-snug text-ink-2 md:table-cell">
          {gist(x)}
          <span className="ml-1 text-xs text-muted">{open ? '▴' : '▾'}</span>
        </td>
      </tr>
      {/* 手機：「要做什麼」放到第二行，整行寬度，不必左右滑 */}
      <tr onClick={onToggle} className="cursor-pointer border-b border-line/70 md:hidden">
        <td colSpan={6} className="px-4 pb-2.5 text-sm leading-snug text-ink-2">
          {gist(x)}<span className="ml-1 text-xs text-muted">{open ? '▴' : '▾'}</span>
        </td>
      </tr>
      {open && (
        <tr className="border-b border-line/70 bg-mist/40">
          <td colSpan={6} className="px-4 py-3 text-sm">
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <div className="mb-1 text-xs font-medium text-muted">理由</div>
                <ul className="space-y-1 pl-4">{(x.reasons ?? []).map((r: string, i: number) => <li key={i} className="list-disc">{r}</li>)}</ul>
              </div>
              <div>
                <div className="mb-1 text-xs font-medium text-muted">什麼情況要改變結論</div>
                <ul className="space-y-1 pl-4">{(x.change_if ?? []).map((r: string, i: number) => <li key={i} className="list-disc">{r}</li>)}</ul>
                {x.stop != null && <p className="mt-1 text-xs text-muted">停損 {x.stop}</p>}
              </div>
            </div>
            <button type="button" onClick={() => ask(`${x.code} ${x.name} 為什麼是「${x.verdict}」？`)}
              className="mt-2 rounded-md border border-line bg-paper px-2.5 py-1 text-xs text-ink hover:border-link">問 AI 為什麼</button>
          </td>
        </tr>
      )}
    </Fragment>
  )
}

/** 買賣決策總表：直接告訴使用者哪些可以買、哪些先不要買、持股為什麼續抱（看盤網站式的分頁表格）。 */
export default function DecisionBoard() {
  const { data: d, isLoading } = useQuery<Json>({
    queryKey: ['decisions'], queryFn: () => get('/api/decisions'), refetchInterval: liveEvery(10_000, 600_000),
  })
  const [tab, setTab] = useState<TabKey | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const all: Json[] = d ? [...d.hold, ...d.buy, ...d.wait, ...d.avoid] : []
  const { data: q } = useQuotes(all.map((x) => x.code))
  const quotes: Json = q?.quotes ?? {}
  const shell = (body: ReactNode) => (
    <section className="mb-6 overflow-hidden rounded-xl border border-line bg-paper shadow-sm" aria-label="能買什麼">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3">
        <h2 className="flex items-center gap-2 text-[1rem] font-bold"><span className="h-4 w-1 rounded-full bg-link" aria-hidden />能買什麼、該抱什麼</h2>
        {d && <span className="text-xs text-muted">{d.time}{d.stale ? '・重算中' : '・每分鐘更新'}</span>}
      </div>
      {body}
    </section>
  )
  if (isLoading) return shell(<p className="px-4 py-6 text-sm text-muted">整理買賣結論中</p>)
  if (!d) return null
  const sells = d.hold.filter((x: Json) => x.verdict === '減碼' || x.verdict === '全部出清').length
  const tabs = [
    ...(d.hold.length ? [{ key: 'hold' as const, label: '手上持股', count: d.hold.length }] : []),
    { key: 'buy' as const, label: '可以買', count: d.buy.length, tone: 'text-up' },
    { key: 'wait' as const, label: '等回檔', count: d.wait.length, tone: 'text-warn' },
    { key: 'avoid' as const, label: '先不要買', count: d.avoid.length },
  ]
  const cur: TabKey = tab ?? tabs[0].key
  const rows: Json[] = d[cur] ?? []
  return shell(
    <>
      <div className="mt-2 flex flex-wrap gap-2 px-4 pb-3 text-sm">
        {sells > 0 && <span className="rounded-md bg-warn/15 px-2 py-0.5 font-medium text-warn">要賣 {sells} 檔</span>}
        <span className="rounded-md bg-up/10 px-2 py-0.5 font-medium text-up">可以買 {d.buy.length} 檔</span>
        <span className="text-ink-2">{d.summary}</span>
      </div>
      <Tabs tabs={tabs} value={cur} onChange={(k) => { setTab(k); setOpen(null) }} />
      {rows.length === 0 ? (
        <p className="px-4 py-6 text-sm text-muted">
          {cur === 'buy' ? '目前沒有價位與資金都合適、可以馬上買的標的。' : '沒有。'}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[0.95rem] md:min-w-[640px]">
            <thead className="bg-mist/60 text-left text-xs text-muted">
              <tr>
                <th className="py-2 pl-4 font-medium">結論</th><th className="font-medium">標的</th>
                <th className="pr-3 text-right font-medium">現價</th><th className="pr-3 text-right font-medium">今日</th>
                <th className="hidden pr-3 text-right font-medium sm:table-cell">{cur === 'hold' ? '持有損益' : '限價'}</th>
                <th className="hidden pr-4 font-medium md:table-cell">要做什麼（點一下看理由）</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((x) => (
                <Row key={x.code} x={x} q={quotes[x.code]} open={open === x.code} onToggle={() => setOpen(open === x.code ? null : x.code)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="px-4 py-2 text-xs text-muted">{d.note}</p>
    </>,
  )
}
