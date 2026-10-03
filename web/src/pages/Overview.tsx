import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import HoldingAdvice from '../components/HoldingAdvice'
import Intraday from '../components/Intraday'
import { IndexTicker, IntlNote, LivePrice } from '../components/LiveBits'
import { BasketTag, Bullets, Empty, ErrorNote, Loading, PageTitle, Section, Stat, StockLink } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtPct, fmtRatio, fmtWan, qty, tone } from '../lib/format'
import { useIntl, useQuotes } from '../lib/live'

function Balance({ alloc }: { alloc: Json }) {
  const off = Math.round(alloc.offense * 100)
  const def = 100 - off
  const reasons: string[] = alloc.reasons.filter((r: string) => !r.startsWith('今日配置'))
  return (
    <section className="mb-8" aria-label="今日攻守配置">
      <div className="mb-2 flex items-end justify-between">
        <div className="title text-[2.4rem] leading-none md:text-[3.2rem]">
          <span className="text-offense">攻 {off}</span>
          <span className="mx-3 text-line">／</span>
          <span className="text-defense">守 {def}</span>
        </div>
        <div className="hidden text-right text-sm text-muted md:block">
          加權 RSI {alloc.taiex.rsi14}　20 日 {fmtPct(alloc.taiex.r20_pct, 1)}　距季線 {fmtPct(alloc.taiex.dist_ma60_pct, 1)}
        </div>
      </div>
      <div className="flex h-4 overflow-hidden rounded-full" role="img" aria-label={`進攻 ${off}%，穩健 ${def}%`}>
        <div className="bg-offense" style={{ width: `${off}%` }} />
        <div className="w-1 bg-mist" />
        <div className="bg-defense" style={{ width: `${def}%` }} />
      </div>
      <div className="mt-1 flex justify-between text-sm">
        <span className="text-offense">進攻籃：追強勢、一個月內創新高</span>
        <span className="text-right text-defense">穩健籃：收正報酬機率高、回檔小</span>
      </div>
      <ol className="mt-3 space-y-1 border-l-2 border-line pl-4 text-[0.95rem] text-ink-2">
        {reasons.map((r, i) => <li key={i}>{r}</li>)}
      </ol>
    </section>
  )
}

function UrgentBanner() {
  const { data } = useQuery<Json>({ queryKey: ['alerts'], queryFn: () => get('/api/alerts'), refetchInterval: 300_000 })
  const items: Json[] = (data?.alerts ?? []).filter((a: Json) => a.level === '嚴重' || a.level === '警告')
  if (!items.length) return null
  return (
    <section className="mb-6 rounded-lg border border-up/40 bg-up/5 px-4 py-3" aria-label="緊急警示">
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <h2 className="font-bold text-up">{items.length} 則警示需要處理</h2>
        <Link to="/alerts" className="text-sm">看全部</Link>
      </div>
      <ul className="space-y-1 text-[0.95rem]">
        {items.slice(0, 4).map((a, i) => (
          <li key={i}><span className={a.level === '嚴重' ? 'font-bold text-up' : 'font-bold text-warn'}>{a.level}</span>　{a.title}：{a.action}</li>
        ))}
      </ul>
    </section>
  )
}

function MorningPlan({ r }: { r: Json }) {
  const codes: string[] = r.orders.map((o: Json) => o.code)
  const { data: q } = useQuotes(codes)
  const { data: intl } = useIntl(codes)
  const quotes: Json = q?.quotes ?? {}
  const notes: Json = intl?.impacts ?? {}
  const buys = r.orders.filter((o: Json) => o.action === 'BUY')
  const sells = r.orders.filter((o: Json) => o.action === 'SELL')
  return (
    <Section title="盤前計畫（07:00）" aside={<Link to={`/daily/${r.key}`}>完整理由與 K 線</Link>}>
      {r.text?.outlook && <p className="mb-3 text-[0.95rem] leading-relaxed text-ink-2">{r.text.outlook}</p>}
      {!r.orders.length && <Empty>今天沒有標的達到進出場條件。</Empty>}
      {sells.length > 0 && (
        <div className="mb-4 space-y-2">
          {sells.map((o: Json) => (
            <div key={o.code} className="rounded-lg border-l-4 border-down bg-paper px-4 py-3">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <span className="font-bold text-down">賣出</span><StockLink code={o.code} name={o.name} />
                <span className="num">{qty(o.lots, o.odd)}</span><span className="num text-muted">限價 {o.limit_price}</span>
                <LivePrice q={quotes[o.code]} />
              </div>
              <div className="mt-1 text-sm text-ink-2">{o.why_sell}</div>
            </div>
          ))}
        </div>
      )}
      {buys.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-[0.95rem]">
            <thead className="text-left text-sm text-muted">
              <tr className="border-b border-line">
                <th className="py-2 font-normal">籃子</th><th className="font-normal">標的</th><th className="text-right font-normal">即時</th>
                <th className="text-right font-normal">數量</th><th className="text-right font-normal">限價</th><th className="text-right font-normal">停損</th>
                <th className="pl-4 font-normal">國際影響</th>
              </tr>
            </thead>
            <tbody>
              {buys.map((o: Json) => (
                <tr key={o.code} className="border-b border-line/70 align-top">
                  <td className="py-2.5"><BasketTag basket={o.basket} /></td>
                  <td className="py-2.5"><Link to={`/daily/${r.key}?code=${o.code}`} className="text-ink no-underline hover:text-link"><span className="num text-muted">{o.code}</span> {o.name}</Link></td>
                  <td className="py-2.5 text-right"><LivePrice q={quotes[o.code]} /></td>
                  <td className="num py-2.5 text-right">{qty(o.lots, o.odd)}</td>
                  <td className="num py-2.5 text-right">{o.limit_price}</td>
                  <td className="num py-2.5 text-right text-down">{o.stop}</td>
                  <td className="py-2.5 pl-4"><IntlNote notes={notes[o.code]} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {intl?.factor_date && (
        <p className="mt-2 text-xs text-muted">國際影響：{intl.factor_date} 國際市場收盤變動 × 該股過去一年與該市場的連動程度（只列統計顯著、影響 ≥0.4% 者）</p>
      )}
    </Section>
  )
}

export default function Overview() {
  const { data: r, isLoading, error } = useQuery<Json>({ queryKey: ['report', 'latest'], queryFn: () => get('/api/report') })
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!r) return <Empty>還沒有任何報告。報告會在交易日 07:00 自動產生。</Empty>
  const pf = r.portfolio
  const tx = r.predictions.TAIEX
  const fails = r.compliance.filter((c: Json) => c.status !== '通過')
  return (
    <>
      <PageTitle aside={<span className="text-sm text-muted">{r.date} {r.title}・產出 {r.generated_at}</span>}>今天怎麼做</PageTitle>
      <IndexTicker />
      <UrgentBanner />
      <Intraday />
      <HoldingAdvice compact />
      <Balance alloc={r.allocation} />

      <div className="mb-8 grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-5">
        <Stat label="總資產" value={fmtWan(pf.nav)} />
        <Stat label="累積報酬" value={fmtPct(pf.return_pct)} valueClass={tone(pf.return_pct)} />
        <Stat label="目前持股率" value={fmtRatio(pf.holding_ratio)} sub="規則下限 70%" />
        <Stat label="執行建議後" value={fmtRatio(r.projected_ratio)} sub="只算實際成交" />
        <Stat label="大盤開盤預測" value={fmtPct(tx?.gap_pct)} valueClass={tone(tx?.gap_pct)}
          sub={tx ? `命中率 ${fmtRatio(tx.gap_hit_rate)}，開盤後${tx.after_open}` : undefined} />
      </div>

      <MorningPlan r={r} />

      <Section title="競賽規則檢查" aside={fails.length ? `${fails.length} 項需注意` : '全部通過'}>
        <ul className="space-y-1.5 text-[0.95rem]">
          {r.compliance.map((c: Json) => (
            <li key={c.item} className="flex gap-2">
              <span className={`shrink-0 font-medium ${c.status === '通過' ? 'text-down' : c.status === '注意' ? 'text-warn' : 'text-up'}`}>{c.status}</span>
              <span><span className="font-medium">{c.item}</span>　<span className="text-ink-2">{c.detail}</span></span>
            </li>
          ))}
        </ul>
      </Section>

      {r.tuner?.issues?.length > 0 && (
        <Section title="調整 Agent 提醒">
          <Bullets items={r.tuner.issues.map((i: Json) => i.detail)} />
        </Section>
      )}
    </>
  )
}
