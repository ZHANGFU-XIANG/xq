import { useQuery } from '@tanstack/react-query'
import { get, type Json } from '../lib/api'
import { liveEvery } from '../lib/live'
import { Section, StockLink } from './ui'

const LEVEL: Record<string, string> = { 極端: 'text-up', 波動升高: 'text-warn', 正常: 'text-ink', 平靜: 'text-down' }

/** 市場異常（波動百分位、寬度、產業偏離、量價異常個股）＋今天新公布的財報。 */
export default function MarketPulse() {
  const { data: a } = useQuery<Json>({ queryKey: ['anomaly'], queryFn: () => get('/api/anomaly'), refetchInterval: liveEvery(60_000, 900_000) })
  const { data: f } = useQuery<Json>({ queryKey: ['fin-reports'], queryFn: () => get('/api/fin-reports'), refetchInterval: 1_800_000 })
  if (!a?.taiex) return null
  const t = a.taiex
  const b = a.breadth ?? {}
  return (
    <Section title="市場異常與財報" aside={a.time}>
      <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-lg border border-line p-3"><div className="text-sm text-muted">市場狀態</div>
          <div className={`text-xl font-bold ${LEVEL[a.level] ?? ''}`}>{a.level}</div>
          <div className="text-xs text-muted">波動率百分位 {t.vol_percentile}（近 10 年）</div></div>
        <div className="rounded-lg border border-line p-3"><div className="text-sm text-muted">加權今天</div>
          <div className="num text-xl font-bold">{t.ret_pct > 0 ? '+' : ''}{t.ret_pct}%</div>
          <div className="text-xs text-muted">約 {t.z > 0 ? '+' : ''}{t.z} 倍平常波動</div></div>
        <div className="rounded-lg border border-line p-3"><div className="text-sm text-muted">漲跌家數</div>
          <div className="num text-xl font-bold"><span className="text-up">{b.up}</span>／<span className="text-down">{b.down}</span></div>
          <div className="text-xs text-muted">漲停 {b.limit_up}・跌停 {b.limit_down}</div></div>
        <div className="rounded-lg border border-line p-3"><div className="text-sm text-muted">成交值 vs 20 日平均</div>
          <div className="num text-xl font-bold">{a.turnover_ratio ?? '—'} 倍</div></div>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <div className="text-sm">
          <div className="mb-1 font-medium">偏離大盤最多的產業</div>
          <ul className="mb-3 space-y-0.5">{(a.sectors ?? []).slice(0, 5).map((s: Json) => (
            <li key={s.sector}>{s.sector} <span className={s.median_pct >= 0 ? 'text-up' : 'text-down'}>{s.median_pct > 0 ? '+' : ''}{s.median_pct}%</span>
              <span className="ml-1 text-xs text-muted">比大盤 {s.vs_market > 0 ? '+' : ''}{s.vs_market}</span></li>))}</ul>
          {a.stocks?.length > 0 && (<>
            <div className="mb-1 font-medium">量價異常個股（≥ 4 倍波動、量 ≥ 3 倍）</div>
            <ul className="mb-3 space-y-0.5">{a.stocks.slice(0, 6).map((s: Json) => (
              <li key={s.code}><StockLink code={s.code} name={s.name} /> {s.chg_pct > 0 ? '+' : ''}{s.chg_pct}%<span className="ml-1 text-xs text-muted">{s.sigma} 倍・量 {s.volume_ratio} 倍</span></li>))}</ul>
          </>)}
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted">{(a.history ?? []).map((h: string) => <li key={h}>{h}</li>)}</ul>
        </div>
        <div className="text-sm">
          <div className="mb-1 font-medium">財報{f ? `（今天新公布 ${f.new_count} 家）` : ''}</div>
          {(f?.new ?? []).filter((d: Json) => d?.flags?.length).slice(0, 8).map((d: Json) => (
            <div key={d.code}><StockLink code={d.code} name={d.name} /> <span className="font-medium">{d.flags.join('、')}</span>
              <div className="text-xs text-muted">{d.text}</div></div>))}
          <div className="mt-2 mb-1 font-medium">持股最新財報</div>
          <ul className="space-y-0.5">{(f?.focus ?? []).slice(0, 14).map((d: Json) => (
            <li key={d.code}>{d.code} {d.name}{d.flags?.length ? <span className="ml-1 font-medium text-warn">{d.flags.join('、')}</span> : null}
              <div className="text-xs text-muted">{d.text}</div></li>))}</ul>
          {f?.note && <p className="mt-2 text-xs text-muted">{f.note}</p>}
        </div>
      </div>
    </Section>
  )
}
