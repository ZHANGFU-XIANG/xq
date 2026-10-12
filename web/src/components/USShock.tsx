import { useQuery } from '@tanstack/react-query'
import { StockLink } from './ui'
import { get, type Json } from '../lib/api'
import { liveEvery } from '../lib/live'

/** 費半重挫預警：一般重挫＝續抱不要殺低；嚴重＝高連動股開盤先賣、第 3 天買回。平常不顯示。 */
export default function USShock({ holdings = false }: { holdings?: boolean }) {
  const { data: d } = useQuery<Json>({ queryKey: ['us-shock'], queryFn: () => get('/api/us-shock'), refetchInterval: liveEvery(60_000, 600_000) })
  if (!d || !['一般重挫', '嚴重'].includes(d.level)) return null
  const severe = d.level === '嚴重'
  const linked: Json[] = (d.items ?? []).filter((r: Json) => r.linked)
  return (
    <section className={`mb-6 rounded-xl border px-5 py-4 shadow-sm ${severe ? 'border-up/50 bg-up/5' : 'border-warn/40 bg-warn/5'}`} aria-label="美股重挫預警">
      <div className={`font-bold ${severe ? 'text-up' : 'text-warn'}`}>{severe ? '美股嚴重重挫：' : '美股重挫：'}{d.headline}</div>
      <p className="mt-1 text-[0.95rem] leading-relaxed">{d.advice}</p>
      {holdings && linked.length > 0 && (
        <ul className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
          {linked.map((r) => (
            <li key={r.code} className="flex justify-between gap-2">
              <span><StockLink code={r.code} name={r.name} /><span className="ml-1 text-muted">β {r.beta}・估 {r.exp_impact_pct > 0 ? '+' : ''}{r.exp_impact_pct}%</span></span>
              <span className={severe ? 'font-medium text-up' : 'text-ink-2'}>{r.action}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-xs text-muted">依據：{d.study.universe}、{d.study.period} 費半單日跌 3% 以上 {d.study.events} 次的事件研究</p>
    </section>
  )
}
