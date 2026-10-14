import { useQuery } from '@tanstack/react-query'
import { createChart, LineSeries, type Time } from 'lightweight-charts'
import { Fragment, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import HoldingAdvice from '../components/HoldingAdvice'
import AssetTabs, { useAssetTab } from '../components/AssetTabs'
import DecisionBoard from '../components/DecisionBoard'
import HoldingWatch from '../components/HoldingWatch'
import OvernightWatch from '../components/OvernightWatch'
import RotationAdvice from '../components/RotationAdvice'
import Intraday from '../components/Intraday'
import USShock from '../components/USShock'
import RegimeBanner from '../components/RegimeBanner'
import XQImport from '../components/XQImport'
import TradeWorkspace, { SEG_LABEL, type Seg } from './Trades'
import { BasketTag, Bullets, Empty, ErrorNote, Loading, PageTitle, Section, Stat, StockLink, Tabs } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtNum, fmtPct, fmtRatio, fmtWan, tone } from '../lib/format'
import { liveEvery } from '../lib/live'

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

type Tab = 'holdings' | 'trades' | 'stats'
const TABS: [Tab, string][] = [['holdings', '持股'], ['trades', '交易紀錄'], ['stats', '統計分析']]

export default function Portfolio() {
  const { data: p, isLoading, error } = useQuery<Json>({ queryKey: ['portfolio'], queryFn: () => get('/api/portfolio'), refetchInterval: liveEvery(15_000, 120_000) })
  const { data: r } = useQuery<Json>({ queryKey: ['report', 'latest'], queryFn: () => get('/api/report') })
  const loc = useLocation()
  const nav = useNavigate()
  const tab: Tab = (['trades', 'stats'] as Tab[]).find((t) => loc.hash === `#${t}`) ?? 'holdings'
  const [seg, setSeg] = useState<Seg>(() => {
    try { return (localStorage.getItem('trade_seg') as Seg) || 'all' } catch { return 'all' }
  })
  const [asset, setAsset] = useAssetTab()
  const pickSeg = (s: Seg) => { setSeg(s); try { localStorage.setItem('trade_seg', s) } catch { /* 無法記住也沒關係 */ } }
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  const b = p.breakdown ?? {}
  const pill = (on: boolean) => `rounded-md px-3 py-1 text-sm ${on ? 'bg-link text-white' : 'border border-line text-ink-2'}`
  const att = r?.attribution
  const maxAtt = Math.max(...(att?.rows ?? []).flatMap((x: Json) => [Math.abs(x.mkt_pct), Math.abs(x.ind_pct), Math.abs(x.idio_pct)]), 0.5)
  return (
    <>
      <PageTitle>持股與交易</PageTitle>
      <div className="mb-6 grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-6">
        <Stat label="投資組合總值（同 App）" value={fmtInt(p.nav)}
          sub={`1,000 萬 ＋ 未實現 ＋ 已實現（手續費不扣）・扣費後淨值 ${fmtInt(p.nav_net)}`} />
        <Stat label="投資報酬率（同 App）" value={fmtPct(p.return_pct, 4)} valueClass={tone(p.return_pct)}
          sub={`扣手續費後 ${fmtPct(p.return_net_pct, 3)}・已付手續費 ${fmtInt(p.app?.fees_paid ?? p.fees_paid)}`} />
        <Stat label="未實現損益（同 App）" value={fmtInt(p.unrealized_total ?? p.unrealized_pnl)} valueClass={tone(p.unrealized_total ?? p.unrealized_pnl)}
          sub={`現股 ${fmtInt(p.unrealized_pnl)}・期貨 ${fmtInt(p.futures_unrealized ?? 0)}（收盤後用結算價）`} />
        <Stat label="持股率（同 App）" value={fmtRatio(p.holding_ratio, 2)} sub={`股票 ${fmtWan(b.stock_mv)}＋ETF ${fmtWan(b.etf_mv)} ÷（市值＋持有現金＋期貨保證金＋期貨未實現）；期貨不算持股，下限 70%`} valueClass={p.holding_ratio < 0.7 ? 'text-warn' : ''} />
        <Stat label="持有現金（同 App）" value={fmtInt(p.app?.cash ?? p.cash)}
          sub={`證券戶 ${fmtInt(p.cash)}＋期貨戶可用 ${fmtInt((p.app?.cash ?? p.cash) - p.cash)}（權益 ${fmtWan(p.futures_equity)} − 保證金 ${fmtWan(p.app?.margin ?? 0)}）`} />
        <Stat label="交易筆數／標的" value={`${p.trade_count}／${p.traded_symbols.length}`} sub="門檻 10 筆、5 檔" />
      </div>

      <RegimeBanner />
      <USShock holdings />

      <div className="mb-5 flex flex-wrap items-end justify-between gap-3 border-b border-line">
        <div className="-mb-px">
          <Tabs value={tab} onChange={(t) => nav({ hash: t === 'holdings' ? '' : t }, { replace: true })}
            tabs={TABS.map(([t, label]) => ({ key: t, label }))} />
        </div>
        {tab !== 'holdings' && (
          <div className="flex flex-wrap gap-1.5 pb-2" role="group" aria-label="篩選">
            {(Object.keys(SEG_LABEL) as Seg[]).map((s) => (
              <button key={s} className={pill(seg === s)} onClick={() => pickSeg(s)}>{SEG_LABEL[s]}</button>
            ))}
          </div>
        )}
      </div>

      {tab !== 'holdings' ? <TradeWorkspace view={tab} seg={seg} /> : (<>
      <OvernightWatch />
      <DecisionBoard />
      <RotationAdvice />
      <HoldingWatch />
      <HoldingAdvice />

      <Section title="目前持股" aside="同一檔分批成交（整股／零股、不同時間）各自列出成本，賣出先進先出">
        {!p.positions.length ? <Empty>還沒有持股。在 XQ 成交後，用下方「從 XQ 匯入」貼上截圖或文字同步，到上方「交易紀錄」分頁手動新增，或跟 AI 助理說「我買了 2330 一張 1000 元」。</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-[0.95rem]">
              <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
                <th className="py-2 font-normal">籃子</th><th className="font-normal">標的</th><th className="text-right font-normal">股數</th><th className="text-right font-normal">成本</th>
                <th className="text-right font-normal">現價</th><th className="text-right font-normal">損益</th><th className="text-right font-normal">權重</th><th className="text-right font-normal">停損</th>
              </tr></thead>
              <tbody>
                {p.positions.map((x: Json) => (
                  <Fragment key={x.code}>
                  <tr className={x.lots?.length > 1 ? '' : 'border-b border-line/70'}>
                    <td className="py-2.5"><BasketTag basket={x.basket} /></td>
                    <td><StockLink code={x.code} name={x.name} /></td>
                    <td className="num text-right">{fmtInt(x.shares)}</td>
                    <td className="num text-right">{fmtNum(x.avg_cost)}</td>
                    <td className="num text-right">{fmtNum(x.price)}</td>
                    <td className={`num text-right ${tone(x.pnl)}`}>{fmtInt(x.pnl)}<div className="text-xs">{fmtPct(x.pnl_pct)}</div></td>
                    <td className="num text-right">{fmtPct(x.weight_pct, 1, false)}</td>
                    <td className="num text-right text-down">{x.stop ?? '—'}</td>
                  </tr>
                  {x.lots?.length > 1 && x.lots.map((l: Json, i: number) => (
                    <tr key={i} className={`text-sm text-ink-2 ${i === x.lots.length - 1 ? 'border-b border-line/70' : ''}`}>
                      <td />
                      <td className="pb-1 pl-4 text-muted">└ {l.date.slice(5)} {l.board}</td>
                      <td className="num pb-1 text-right">{fmtInt(l.shares)}</td>
                      <td className="num pb-1 text-right">{fmtNum(l.unit_cost)}</td>
                      <td />
                      <td className={`num pb-1 text-right ${tone(l.pnl)}`}>{fmtInt(l.pnl)}<span className="ml-1 text-xs">{fmtPct(l.pnl_pct)}</span></td>
                      <td colSpan={2} />
                    </tr>
                  ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {p.derivatives?.length > 0 && (
        <Section title="期貨與選擇權部位" aside={`期貨戶權益數 ${fmtInt(p.futures_equity)} 元`}>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-[0.95rem]">
              <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
                <th className="py-2 font-normal">類別</th><th className="font-normal">合約</th><th className="text-right font-normal">口數</th>
                <th className="text-right font-normal">均價</th><th className="text-right font-normal">現價</th>
                <th className="text-right font-normal">未實現損益／市值</th><th className="text-right font-normal">保證金</th>
              </tr></thead>
              <tbody>
                {p.derivatives.map((x: Json) => (
                  <tr key={x.contract} className="border-b border-line/70">
                    <td className="py-2.5 text-fut">{x.kind}</td>
                    <td>{x.contract}</td>
                    <td className="num text-right">{x.qty > 0 ? '多 ' : '空 '}{Math.abs(x.qty)}</td>
                    <td className="num text-right">{fmtNum(x.avg_price)}</td>
                    <td className="num text-right">{x.price != null ? fmtNum(x.price) : '—'}</td>
                    <td className={`num text-right ${tone(x.value)}`}>{fmtInt(x.value)}</td>
                    <td className="num text-right">{x.margin ? fmtInt(x.margin) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-muted">期貨記錄方式：跟 AI 助理說「我買了微台 TMF202610 2 口 48000 點」；資金調撥說「從證券戶轉 50 萬到期貨戶」。</p>
        </Section>
      )}

      <Section title="從 XQ 匯入持股與成交" aside="截圖或貼上，以 XQ 帳務為準"><XQImport /></Section>

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

      <AssetTabs tab={asset} onChange={setAsset} />
      <Intraday asset={asset} />

      {p.compliance?.length > 0 && (
        <Section title="競賽規則檢查" aside={p.compliance.some((c: Json) => c.status !== '通過') ? '有項目需注意' : '全部通過'}>
          <ul className="space-y-1.5 text-[0.95rem]">
            {p.compliance.map((c: Json) => (
              <li key={c.item} className="flex gap-2">
                <span className={`shrink-0 font-medium ${c.status === '通過' ? 'text-down' : c.status === '注意' ? 'text-warn' : 'text-up'}`}>{c.status}</span>
                <span><span className="font-medium">{c.item}</span>　<span className="text-ink-2">{c.detail}</span></span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      </>)}
    </>
  )
}
