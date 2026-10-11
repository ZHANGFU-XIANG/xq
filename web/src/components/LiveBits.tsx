import type { Json } from '../lib/api'
import { useQuotes } from '../lib/live'
import { tone } from '../lib/format'
import { Change } from './ui'

/** 台股指數即時列：加權、櫃買。 */
export function IndexTicker() {
  const { data } = useQuotes([])
  const idx: Json = data?.indices ?? {}
  if (!Object.keys(idx).length) return null
  return (
    <div className="mb-6 flex flex-wrap items-baseline gap-x-8 gap-y-2 rounded-lg bg-paper px-4 py-3" aria-live="polite">
      {Object.entries(idx).map(([k, v]: [string, Json]) => (
        <div key={k} className="flex items-baseline gap-2">
          <span className="text-sm text-muted">{v.name}</span>
          <span className={`num text-2xl font-bold ${tone(v.change)}`}>{v.price.toLocaleString('zh-TW', { minimumFractionDigits: 2 })}</span>
          <span className={`num ${tone(v.change)}`}>{v.change > 0 ? '+' : ''}{v.change.toFixed(2)}</span>
          <Change value={v.chg_pct} />
        </div>
      ))}
      <span className="ml-auto text-xs text-muted">
        {data?.trading ? '盤中即時（每 10 秒）' : '收盤'}・{(Object.values(idx)[0] as Json)?.time ?? ''}
      </span>
    </div>
  )
}

/** 每頁頂端的行情列（像看盤網站）：加權、櫃買即時指數與盤中／收盤狀態，桌機版固定在畫面上方。 */
export function MarketBar() {
  const { data } = useQuotes([])
  const idx: Json = data?.indices ?? {}
  const first = Object.values(idx)[0] as Json | undefined
  return (
    <div className="border-b border-line bg-paper/95 backdrop-blur md:sticky md:top-0 md:z-10" aria-live="polite">
      <div className="mx-auto flex max-w-7xl items-center gap-x-6 gap-y-1 overflow-x-auto px-4 py-2 [scrollbar-width:none] md:px-6">
        {Object.entries(idx).map(([k, v]: [string, Json]) => (
          <div key={k} className="flex shrink-0 items-baseline gap-2">
            <span className="text-xs text-muted">{v.name}</span>
            <span className={`num text-[1.05rem] font-bold ${tone(v.change)}`}>{v.price.toLocaleString('zh-TW', { minimumFractionDigits: 2 })}</span>
            <span className={`num text-sm ${tone(v.change)}`}>{v.change > 0 ? '+' : ''}{v.change.toFixed(2)}</span>
            <span className="text-sm"><Change value={v.chg_pct} /></span>
          </div>
        ))}
        {!Object.keys(idx).length && <span className="text-xs text-muted">載入行情中</span>}
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs text-muted">
          <span className={`h-2 w-2 rounded-full ${data?.trading ? 'animate-pulse bg-up' : 'bg-muted'}`} aria-hidden />
          {data?.trading ? '盤中即時' : '已收盤'}{first?.time ? `・${first.time}` : ''}
        </span>
      </div>
    </div>
  )
}

/** 單一股票的即時價格與漲跌。 */
export function LivePrice({ q }: { q?: Json }) {
  if (!q?.price) return <span className="text-muted">—</span>
  return (
    <span className="num whitespace-nowrap">
      <span className={tone(q.chg_pct)}>{q.price}</span> <Change value={q.chg_pct} />
      {q.at_limit_up && <span className="ml-1 rounded bg-up px-1 text-xs text-white">漲停</span>}
      {q.at_limit_down && <span className="ml-1 rounded bg-down px-1 text-xs text-white">跌停</span>}
    </span>
  )
}

/** 國際影響註記（美股／日股等）。 */
export function IntlNote({ notes }: { notes?: Json[] }) {
  if (!notes?.length) return null
  return (
    <span className="flex flex-wrap gap-1">
      {notes.map((n: Json, i: number) => (
        <span key={i} title={n.text}
          className={`rounded px-1.5 py-0.5 text-xs ${n.impact_pct > 0 ? 'bg-up/10 text-up' : 'bg-down/10 text-down'}`}>
          {n.region}｜{n.factor} {n.move} → {n.impact_pct > 0 ? '+' : ''}{n.impact_pct.toFixed(1)}%
        </span>
      ))}
    </span>
  )
}
