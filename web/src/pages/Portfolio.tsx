import { useQuery } from '@tanstack/react-query'
import { createChart, LineSeries, type Time } from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { BasketTag, Bullets, Empty, ErrorNote, Loading, PageTitle, Section, Stat, StockLink } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtNum, fmtPct, fmtRatio, fmtWan, tone } from '../lib/format'

function NavCurve({ hist }: { hist: Json[] }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!box.current || hist.length < 2) return
    const v = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()
    const chart = createChart(box.current, {
      height: 220, autoSize: true, layout: { background: { color: 'transparent' }, textColor: v('--color-ink-2') },
      grid: { vertLines: { visible: false }, horzLines: { color: v('--color-line') } },
    })
    const s = chart.addSeries(LineSeries, { color: v('--color-link'), lineWidth: 2 })
    s.setData(hist.map((h) => ({ time: h.date as Time, value: h.return_pct })))
    s.createPriceLine({ price: 0, color: v('--color-muted'), lineWidth: 1, title: '0%' })
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [hist])
  if (hist.length < 2) return <p className="text-muted">累積兩個交易日以上的盤後紀錄後會顯示曲線。</p>
  return <div ref={box} style={{ height: 220 }} aria-label="累積報酬率走勢" />
}

function Bar({ v, max }: { v: number; max: number }) {
  const w = Math.min(50, (Math.abs(v) / (max || 1)) * 50)
  return (
    <div className="relative h-3 w-full bg-mist" aria-hidden>
      <div className="absolute inset-y-0 left-1/2 w-px bg-line" />
      <div className={`absolute inset-y-0 ${v >= 0 ? 'left-1/2 bg-up' : 'right-1/2 bg-down'}`} style={{ width: `${w}%` }} />
    </div>
  )
}

export default function Portfolio() {
  const { data: p, isLoading, error } = useQuery<Json>({ queryKey: ['portfolio'], queryFn: () => get('/api/portfolio') })
  const { data: r } = useQuery<Json>({ queryKey: ['report', 'latest'], queryFn: () => get('/api/report') })
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  const att = r?.attribution
  const maxAtt = Math.max(...(att?.rows ?? []).flatMap((x: Json) => [Math.abs(x.mkt_pct), Math.abs(x.ind_pct), Math.abs(x.idio_pct)]), 0.5)
  return (
    <>
      <PageTitle>持股與損益</PageTitle>
      <div className="mb-8 grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-5">
        <Stat label="總資產" value={fmtWan(p.nav)} />
        <Stat label="累積報酬" value={fmtPct(p.return_pct)} valueClass={tone(p.return_pct)} />
        <Stat label="持股率" value={fmtRatio(p.holding_ratio)} sub="規則下限 70%" valueClass={p.holding_ratio < 0.7 ? 'text-warn' : ''} />
        <Stat label="證券戶現金" value={fmtWan(p.cash)} sub={`期貨戶 ${fmtWan(p.futures_cash)}`} />
        <Stat label="交易筆數／標的" value={`${p.trade_count}／${p.traded_symbols.length}`} sub="門檻 10 筆、5 檔" />
      </div>

      <Section title="目前持股">
        {!p.positions.length ? <Empty>還沒有持股。在競賽平台成交後，到 AI 助理說「我買了 2330 一張 1000 元」即可記錄。</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-[0.95rem]">
              <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
                <th className="py-2 font-normal">籃子</th><th className="font-normal">標的</th><th className="text-right font-normal">股數</th><th className="text-right font-normal">成本</th>
                <th className="text-right font-normal">現價</th><th className="text-right font-normal">損益</th><th className="text-right font-normal">權重</th><th className="text-right font-normal">停損</th>
              </tr></thead>
              <tbody>
                {p.positions.map((x: Json) => (
                  <tr key={x.code} className="border-b border-line/70">
                    <td className="py-2.5"><BasketTag basket={x.basket} /></td>
                    <td><StockLink code={x.code} name={x.name} /></td>
                    <td className="num text-right">{fmtInt(x.shares)}</td>
                    <td className="num text-right">{fmtNum(x.avg_cost)}</td>
                    <td className="num text-right">{fmtNum(x.price)}</td>
                    <td className={`num text-right ${tone(x.pnl)}`}>{fmtInt(x.pnl)}<div className="text-xs">{fmtPct(x.pnl_pct)}</div></td>
                    <td className="num text-right">{fmtPct(x.weight_pct, 1, false)}</td>
                    <td className="num text-right text-down">{x.stop ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="累積報酬走勢"><NavCurve hist={p.nav_history} /></Section>

      {att?.rows?.length > 0 && (
        <Section title={`${att.date} 漲跌歸因${r.hypothetical ? '（尚無持股，以候選股示範）' : ''}`} aside={`加權指數 ${fmtPct(att.taiex_ret_pct)}`}>
          <div className="space-y-4">
            {att.rows.map((x: Json) => (
              <div key={x.code}>
                <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
                  <StockLink code={x.code} name={x.name} />
                  <span className="text-sm text-ink-2">{x.verdict}</span>
                </div>
                <div className="grid grid-cols-[5rem_1fr_4rem] items-center gap-x-3 gap-y-1 text-sm">
                  {[['大盤', x.mkt_pct], ['產業', x.ind_pct], ['個股', x.idio_pct]].map(([k, val]) => (
                    <div key={k as string} className="contents">
                      <span className="text-muted">{k}</span><Bar v={val as number} max={maxAtt} />
                      <span className={`num text-right ${tone(val as number)}`}>{fmtPct(val as number)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {r?.recovery && (
        <Section title="虧損補回計畫">
          <p className="mb-3 leading-relaxed">{r.text?.recovery_plan}</p>
          <Bullets items={r.recovery.summary} />
          {r.recovery.items.map((it: Json) => (
            <div key={it.code} className="mt-4">
              <h3 className="font-bold">{it.code} {it.name}：{it.decision}</h3>
              <Bullets items={it.lines} />
            </div>
          ))}
        </Section>
      )}

      <Section title="交易紀錄">
        {!p.trades.length ? <p className="text-muted">尚無交易紀錄。</p> : (
          <table className="w-full text-[0.95rem]">
            <tbody>
              {p.trades.map((t: Json, i: number) => (
                <tr key={i} className="border-b border-line/60">
                  <td className="num py-1.5 text-muted">{t.date}</td>
                  <td className={t.side === 'BUY' ? 'text-up' : 'text-down'}>{t.side === 'BUY' ? '買進' : '賣出'}</td>
                  <td><StockLink code={t.code} /></td>
                  <td className="num text-right">{fmtInt(Number(t.shares))} 股</td>
                  <td className="num text-right">@ {t.price}</td>
                  <td className="text-right text-sm text-muted">{t.margin === '1' ? '融資' : ''} {t.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  )
}
