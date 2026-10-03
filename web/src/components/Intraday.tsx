import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, type Json } from '../lib/api'
import { Change } from './ui'

const STATUS_STYLE: Record<string, string> = {
  可以下單: 'bg-up text-white', 已成交: 'bg-down text-white', 接近限價: 'bg-mist text-ink-2', 不追價: 'bg-mist text-ink-2',
  漲停買不到: 'bg-warn/20 text-warn', 跌停賣不掉: 'bg-warn/20 text-warn', 無報價: 'bg-mist text-muted',
}

/** 盤中建議（交易時段每 2 分鐘更新；GitHub Actions 另外每小時寄信）。 */
export default function Intraday() {
  const qc = useQueryClient()
  const { data: d, isFetching } = useQuery<Json>({ queryKey: ['intraday'], queryFn: () => get('/api/intraday'), refetchInterval: 120_000 })
  if (!d) return null
  const hasContent = d.orders.length || d.actions.length || d.holding_alerts.length
  if (!hasContent && !d.ratio_note) return null
  return (
    <section className="mb-8 rounded-lg border border-line bg-paper p-4" aria-label="盤中建議">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-bold">盤中建議 <span className="text-sm font-normal text-muted">{d.time}{d.is_last_check ? '・收盤前檢查' : ''}</span></h2>
        <span className="flex items-center gap-3 text-sm text-muted">
          {d.taiex && <span>加權 {d.taiex.price} <Change value={d.taiex.chg_pct} /></span>}
          <span>實際持股率 <b className={d.holding_ratio < 0.7 ? 'text-warn' : 'text-ink'}>{(d.holding_ratio * 100).toFixed(1)}%</b></span>
          <button disabled={isFetching} onClick={() => get('/api/intraday?refresh=true').then((v) => qc.setQueryData(['intraday'], v))}
            className="rounded-md border border-line px-2 py-0.5 text-ink disabled:opacity-50">更新</button>
        </span>
      </div>
      <p className="title mb-3 text-lg">{d.summary}</p>
      {d.actions.length > 0 && (
        <ul className="mb-3 space-y-1.5">
          {d.actions.map((a: string, i: number) => <li key={i} className="rounded-md bg-up/5 px-3 py-1.5 font-medium">{a}</li>)}
        </ul>
      )}
      {d.orders.length > 0 && (
        <ul className="divide-y divide-line/60">
          {d.orders.map((o: Json) => (
            <li key={o.code + o.action} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2 text-[0.95rem]">
              <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_STYLE[o.status] ?? 'bg-mist'}`}>{o.status}</span>
              <span className="font-medium">{o.action === 'BUY' ? '買' : '賣'} <span className="num text-muted">{o.code}</span> {o.name}</span>
              {o.price != null && <span className="num text-sm">現價 {o.price} <Change value={o.chg_pct} /></span>}
              <span className="w-full text-sm text-ink-2 md:w-auto">{o.advice}</span>
            </li>
          ))}
        </ul>
      )}
      {d.ratio_note && !d.actions.some((a: string) => a.startsWith('【持股率】')) && <p className="mt-2 text-sm text-ink-2">{d.ratio_note}</p>}
    </section>
  )
}
