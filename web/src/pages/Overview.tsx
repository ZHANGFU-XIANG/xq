import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import RegimeBanner from '../components/RegimeBanner'
import TopPicks from '../components/TopPicks'
import Events from '../components/Events'
import USShock from '../components/USShock'
import ValuePicks from '../components/ValuePicks'
import { useChat } from '../components/Chat'
import { ErrorNote, Loading, PageTitle, Section, Stat, StockLink } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtPct, fmtRatio } from '../lib/format'
import { liveEvery } from '../lib/live'

/** 當沖排行前 5 名（完整的在當沖頁）。 */
function DayTradeMini() {
  const { data: d } = useQuery<Json>({ queryKey: ['daytrade'], queryFn: () => get('/api/daytrade'), refetchInterval: liveEvery(5_000, 300_000) })
  const rows: Json[] = (d?.rows ?? []).slice(0, 5)
  return (
    <Section title="當沖排行" aside={<Link to="/daytrade">看全部</Link>}>
      {!d ? <Loading label="計算中" /> : (
        <ul className="space-y-1.5 text-[0.95rem]">
          {rows.map((r) => (
            <li key={r.code} className="flex items-center justify-between gap-2">
              <span className="min-w-0 truncate"><span className="num mr-1 text-muted">{r.rank}</span><StockLink code={r.code} name={r.name} /></span>
              <span className={`shrink-0 text-xs ${r.verdict.startsWith('可以當沖') ? 'font-bold text-up' : 'text-muted'}`}>{r.verdict}</span>
            </li>
          ))}
          <li className="pt-1 text-xs text-muted">{d.phase}・{d.time.slice(11, 16)}</li>
        </ul>
      )}
    </Section>
  )
}

export default function Overview() {
  const chat = useChat()
  const [cap, setCap] = useState(() => { try { return Number(localStorage.getItem('picks_cap')) || 0 } catch { return 0 } })
  const pickCap = (v: number) => { setCap(v); try { localStorage.setItem('picks_cap', String(v)) } catch { /* 無法記住也沒關係 */ } }
  const qs = cap ? `?max_price=${cap}` : ''
  const { data: d, isLoading, error } = useQuery<Json>({
    queryKey: ['picks', qs], queryFn: () => get(`/api/picks${qs}`), refetchInterval: liveEvery(10_000, 300_000),
    placeholderData: (prev: Json) => prev,
  })
  const bt = d?.backtest ?? {}
  const top = d?.items?.[0]
  return (
    <>
      <PageTitle aside={<button onClick={() => chat.ask('現在推薦買什麼？報酬最高的是哪幾檔？')} className="rounded-md bg-link px-4 py-2 text-sm font-medium text-white hover:opacity-90">問 AI</button>}>
        高報酬推薦・綜合排名
      </PageTitle>
      {isLoading && <Loading label="計算全市場排行（首次約需 1 分鐘）" />}
      {error && <ErrorNote error={error} />}
      {d && (
        <>
          <USShock />
          <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="第一名" value={top ? <span className="text-xl">{top.name}</span> : '—'}
              sub={top ? `綜合預期 ${fmtPct(top.score, 1)}・勝率 ${fmtRatio(top.p_up, 0)}・可信度${top.confidence}` : undefined} />
            <Stat label={`回測：前 ${bt.topk} 名 20 日報酬`} value={fmtPct((bt.topk_ret ?? 0) * 100, 1)} valueClass="text-up"
              sub={`全市場 ${fmtPct((bt.mkt_ret ?? 0) * 100, 1)}・樣本外 ${bt.folds?.length ?? 0} 期平均`} />
            <Stat label="回測：一個月內創新高" value={fmtRatio(bt.topk_newhigh, 0)} sub={`全市場 ${fmtRatio(bt.base_rate, 0)}`} />
            <Stat label="排序範圍" value={`${d.n_universe} 檔`} sub={`資料日 ${d.asof}・${d.time.slice(11, 16)} 更新`} />
          </div>
          <div className="grid items-start gap-6 lg:grid-cols-3">
            <div className="min-w-0 lg:col-span-2">
              <Section title="綜合排名" aside={d.trading ? '盤中每 20 秒更新' : '收盤後'} className="[&>div:last-child]:p-0">
                <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-3 text-sm" role="group" aria-label="股價上限">
                  <span className="mr-1">股價</span>
                  {[0, 50, 100, 200, 500, 1000].map((v) => (
                    <button key={v} onClick={() => pickCap(v)}
                      className={`rounded-md px-3 py-1 ${cap === v ? 'bg-link text-white' : 'border border-line text-ink-2 hover:text-ink'}`}>
                      {v ? `${v} 元內` : '不限'}
                    </button>
                  ))}
                  <span className="ml-1 text-muted">
                    {d.n_events > 0 && `・納入 ${d.n_events} 檔今日資訊事件`}{d.n_choppy > 0 && `・排除 ${d.n_choppy} 檔走勢鋸齒／成交值偏低`}
                  </span>
                </div>
                <div className="p-4"><TopPicks items={d.items} /></div>
              </Section>
              <p className="-mt-3 mb-2 text-sm leading-relaxed text-muted">{d.method}</p>
              <p className="mb-6 text-sm leading-relaxed text-warn">實際勝率（2002～2026 樣本外，隔日開盤買、20 天後賣、扣成本）：單一推薦賺錢機率約 50%、平均 +1.5～2.5%；前 5 名分散買約 56%。優勢來自「賺的時候賺比較多」，所以請分散買 5 檔以上、照停損，不要只押一檔。</p>
            </div>
            <div className="min-w-0 lg:sticky lg:top-4">
              <RegimeBanner compact />
              <DayTradeMini />
            </div>
          </div>
          <Events />
          <ValuePicks />
        </>
      )}
    </>
  )
}
