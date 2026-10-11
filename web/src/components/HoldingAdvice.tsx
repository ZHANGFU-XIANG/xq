import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtPct, tone } from '../lib/format'
import { BasketTag, Bullets } from './ui'
import { VERDICT_STYLE } from './DecisionBoard'
import { liveEvery } from '../lib/live'


/** 庫存中每一檔的結論：加碼／續抱／減碼／全部出清（依目前庫存與即時價計算，XQ 同步後立即更新）。 */
export default function HoldingAdvice({ compact = false, asset }: { compact?: boolean; asset?: 'STOCK' | 'ETF' }) {
  const qc = useQueryClient()
  const { data: d, isLoading, isFetching } = useQuery<Json>({
    queryKey: ['holding-advice'], queryFn: () => get('/api/holding-advice'), refetchInterval: liveEvery(60_000, 300_000),
  })
  if (isLoading) return <p className="py-4 text-muted">計算持股建議中（首次約 30 秒）</p>
  if (!d) return null
  const items: Json[] = d.items.filter((h: Json) => !asset || (/^00\d/.test(h.code) ? 'ETF' : 'STOCK') === asset)
  if (!items.length) return compact ? null : <p className="text-muted">{d.note}</p>
  return (
    <section className="mb-6" aria-label="庫存怎麼處理">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2 border-b border-line pb-1.5">
        <h2 className="text-[1.05rem] font-bold">庫存怎麼處理</h2>
        <span className="flex items-center gap-3 text-sm text-muted">
          {Object.entries(d.counts).filter(([, n]) => (n as number) > 0).map(([k, n]) => `${k} ${n}`).join('・')}
          <span>{d.realtime ? '即時價' : '收盤價'}・{d.time}</span>
          <button disabled={isFetching} onClick={() => get('/api/holding-advice?refresh=true').then((v) => qc.setQueryData(['holding-advice'], v))}
            className="rounded-md border border-line px-2 py-0.5 text-ink disabled:opacity-50">重新計算</button>
        </span>
      </div>
      <div className="space-y-3">
        {items.map((h: Json) => (
          <article key={h.code} className="rounded-lg border border-line bg-paper px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className={`rounded-md px-2.5 py-0.5 text-sm font-bold ${VERDICT_STYLE[h.verdict]}`}>{h.verdict}</span>
              <Link to={`/stock/${h.code}`} className="font-bold text-ink no-underline hover:text-link"><span className="num text-muted">{h.code}</span> {h.name}</Link>
              <BasketTag basket={h.basket} />
              <span className="num text-sm text-ink-2">
                {fmtInt(h.shares)} 股・成本 {h.avg_cost}・現價 {h.price}・<span className={tone(h.pnl)}>{fmtInt(h.pnl)} 元（{fmtPct(h.pnl_pct)}）</span>・權重 {fmtPct(h.weight_pct, 1, false)}
              </span>
            </div>
            <p className="title mt-2 text-[1.05rem]">
              {h.qty_text}{h.limit_price ? `，限價 ${h.limit_price}` : ''}
            </p>
            {!compact && (
              <div className="mt-2 grid gap-4 md:grid-cols-2">
                <div><h3 className="mb-1 text-sm font-medium text-muted">理由</h3><Bullets items={h.reasons} /></div>
                <div><h3 className="mb-1 text-sm font-medium text-muted">接下來的條件</h3><Bullets items={h.conditions} /></div>
              </div>
            )}
          </article>
        ))}
      </div>
      {compact && <Link to="/portfolio" className="mt-2 inline-block text-sm">看每檔的理由與條件</Link>}
    </section>
  )
}
