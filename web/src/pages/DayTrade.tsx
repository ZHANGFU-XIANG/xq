import { useQuery } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { useChat } from '../components/Chat'
import { Bullets, Change, Empty, ErrorNote, Loading, PageTitle, Section, Stat, StockLink, Tabs } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtPct, tone } from '../lib/format'
import { liveEvery } from '../lib/live'

type Tab = 'all' | 'go' | 'signal' | 'wait'

const VERDICT_STYLE: Record<string, string> = {
  可以當沖做多: 'bg-up text-white',
  可以當沖做空: 'bg-down text-white',
  等突破再進: 'bg-warn/15 text-warn',
  等回檔再進: 'bg-warn/15 text-warn',
  開盤後觀察: 'bg-mist text-ink-2',
  準備平倉: 'bg-mist text-ink-2',
  等打開再進: 'bg-mist text-ink-2',
}

function Verdict({ v }: { v: string }) {
  return <span className={`inline-flex shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${VERDICT_STYLE[v] ?? 'bg-mist'}`}>{v}</span>
}

function Rows({ rows }: { rows: Json[] }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!rows.length) return <Empty>目前沒有符合的股票</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[860px] text-[0.95rem]">
        <thead className="text-left text-sm text-muted">
          <tr className="border-b border-line">
            <th className="py-2 font-normal">排名</th><th className="font-normal">股票</th><th className="font-normal">結論</th>
            <th className="text-right font-normal">現價</th><th className="text-right font-normal">漲跌</th><th className="text-right font-normal">距開盤</th>
            <th className="text-right font-normal">量比</th><th className="text-right font-normal">日均波動</th><th className="text-right font-normal">均成交值</th>
            <th className="text-right font-normal">進場／停損／目標</th><th className="text-right font-normal">張數</th><th className="text-right font-normal">到目標／觸停損</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <Fragment key={r.code}>
              <tr className="cursor-pointer border-b border-line/60 hover:bg-mist" onClick={() => setOpen(open === r.code ? null : r.code)}>
                <td className="num py-1.5 text-muted">{r.rank}</td>
                <td><StockLink code={r.code} name={r.name} />{r.signal && <span className="ml-1.5 text-xs text-warn">{r.signal}</span>}</td>
                <td><Verdict v={r.verdict} /></td>
                <td className="num text-right">{r.price ?? r.close ?? '—'}</td>
                <td className="text-right"><Change value={r.chg_pct} /></td>
                <td className={`num text-right ${tone(r.from_open_pct)}`}>{fmtPct(r.from_open_pct)}</td>
                <td className="num text-right">{r.volume_ratio ?? '—'}</td>
                <td className="num text-right">{r.atr_pct == null ? '—' : `${r.atr_pct.toFixed(1)}%`}</td>
                <td className="num text-right">{r.val20_e8 == null ? '—' : `${fmtInt(r.val20_e8)} 億`}</td>
                <td className="num text-right text-sm">{r.entry ? `${r.entry}／${r.stop}／${r.target}` : r.trigger ? `突破 ${r.trigger}` : '—'}</td>
                <td className="text-right text-sm">{r.shares ? r.qty_text.replace(/（.*）/, '') : '—'}</td>
                <td className="num text-right text-sm">{r.shares ? <><span className="text-up">{fmtInt(r.profit_at_target)}</span>／<span className="text-down">{fmtInt(r.loss_at_stop)}</span></> : '—'}</td>
              </tr>
              {open === r.code && (
                <tr className="border-b border-line/60 bg-mist/60">
                  <td />
                  <td colSpan={11} className="py-2 pr-2 text-sm leading-relaxed text-ink-2">
                    {r.plan && <div className="mb-1 font-medium text-ink">{r.plan}</div>}
                    <div>{(r.reasons ?? []).join('；')}</div>
                    {r.levels && <div className="mt-1"><span className="font-medium text-ink">技術價位：</span>{r.levels.text}</div>}
                    {r.size_note && <div className="mt-1 text-warn">{r.size_note}</div>}
                    {r.net_reward_pct != null && <div className="mt-1 text-muted">到目標扣成本 +{r.net_reward_pct}%、觸停損含成本 −{r.net_risk_pct}%{r.short_ok ? '' : '；此股暫停先賣後買，只能先買後賣'}</div>}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">點一下列可展開理由、操作計畫與技術價位（買點、賣點、停損）；損益已扣手續費與證交稅。
        技術價位未經分鐘資料驗證：日 K 回測掛支撐價買進後抱到收盤平均是虧的，買到就要在賣點或收盤前出場。</p>
    </div>
  )
}

function Backtest({ bt }: { bt: Json }) {
  if (!bt) return <p className="text-sm text-muted">歷史資料不足，暫無回測。</p>
  const line = (label: string, x: Json) => x && (
    <tr className="border-b border-line/60" key={label}>
      <td className="py-1.5">{label}</td>
      <td className="num text-right">{fmtInt(x.trades)}</td>
      <td className={`num text-right ${tone(x.avg_net_pct)}`}>{fmtPct(x.avg_net_pct, 3)}</td>
      <td className="num text-right">{(x.win_rate * 100).toFixed(0)}%</td>
    </tr>
  )
  const sig = (label: string, x: Json) => x && [line(`${label}（先停損，保守）`, x.worst), line(`${label}（先停利，樂觀）`, x.best)]
  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[480px] text-[0.95rem]">
          <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
            <th className="py-2 font-normal">規則（{bt.from}～{bt.to}）</th><th className="text-right font-normal">筆數</th>
            <th className="text-right font-normal">每筆平均（扣成本）</th><th className="text-right font-normal">勝率</th>
          </tr></thead>
          <tbody>
            {sig('盤中做多訊號', bt.long)}
            {sig('盤中做空訊號', bt.short)}
            {line('排行前 10 名開盤買、收盤賣', bt.open_close)}
            {line('對照：所有合格股票開盤買、收盤賣', bt.baseline)}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-sm leading-relaxed text-ink-2">{bt.method}</p>
    </>
  )
}

function Lookup() {
  const [q, setQ] = useState('')
  const [code, setCode] = useState('')
  const { data, isFetching, error } = useQuery<Json>({ queryKey: ['daytrade', code], queryFn: () => get(`/api/daytrade/${code}`), enabled: !!code })
  return (
    <div>
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); setCode(q.trim()) }}>
        <label htmlFor="dt-code" className="sr-only">股票代號</label>
        <input id="dt-code" value={q} onChange={(e) => setQ(e.target.value)} placeholder="輸入代號，例如 2603"
          className="min-w-0 flex-1 rounded-md border border-line bg-paper px-3 py-1.5 text-sm outline-none focus:border-link" />
        <button className="rounded-md bg-link px-4 py-1.5 text-sm font-medium text-white hover:opacity-90" disabled={!q.trim()}>查詢</button>
      </form>
      {isFetching && <Loading label="計算中" />}
      {error && <ErrorNote error={error} />}
      {data && !isFetching && (
        <div className="mt-3 rounded-lg border border-line p-3 text-sm leading-relaxed">
          <div className="mb-1 flex flex-wrap items-center gap-2"><StockLink code={data.code} name={data.name} /><Verdict v={data.verdict} />
            {data.daily_rank && <span className="text-muted">當沖適合度第 {data.daily_rank}／{data.daily_total} 名</span>}</div>
          {data.plan && <div className="font-medium">{data.plan}</div>}
          <div className="text-ink-2">{(data.reasons ?? []).join('；')}</div>
          {data.levels && <div className="mt-1"><span className="font-medium">技術價位：</span>{data.levels.text}</div>}
          {data.levels && <div className="mt-1 text-xs text-muted">{data.levels.note}</div>}
        </div>
      )}
    </div>
  )
}

export default function DayTrade() {
  const chat = useChat()
  const [tab, setTab] = useState<Tab>('all')
  const [cap, setCap] = useState(() => { try { return Number(localStorage.getItem('daytrade_cap')) || 0 } catch { return 0 } })
  const pickCap = (v: number) => { setCap(v); try { localStorage.setItem('daytrade_cap', String(v)) } catch { /* 無法記住也沒關係 */ } }
  const { data: d, isLoading, error } = useQuery<Json>({
    queryKey: ['daytrade', 'board', cap], queryFn: () => get(`/api/daytrade${cap ? `?max_price=${cap}` : ''}`),
    refetchInterval: liveEvery(15_000, 300_000), placeholderData: (prev: Json) => prev,
  })
  const rows: Json[] = d?.rows ?? []
  const lists: Record<Tab, Json[]> = {
    all: rows,
    go: rows.filter((r) => r.verdict.startsWith('可以當沖')),
    signal: rows.filter((r) => r.signal),
    wait: rows.filter((r) => r.verdict === '等突破再進'),
  }
  const go = d?.verdict?.startsWith('可以當沖') || d?.verdict?.startsWith('當沖推薦')
  return (
    <>
      <PageTitle aside={<button onClick={() => chat.ask('今天有什麼適合當沖的？能不能當沖？')} className="rounded-md bg-link px-4 py-2 text-sm font-medium text-white hover:opacity-90">問 AI</button>}>
        當沖
      </PageTitle>
      {isLoading && <Loading label="計算當沖排行（首次約需 1 分鐘）" />}
      {error && <ErrorNote error={error} />}
      {d && (
        <>
          <section className={`mb-5 rounded-xl border px-5 py-4 shadow-sm ${go ? 'border-up/40 bg-up/5' : 'border-line bg-paper'}`} aria-label="結論">
            <div className="text-sm text-muted">{d.phase}・{d.time.slice(11, 16)} 更新・{d.phase_note}</div>
            <div className="mt-1 text-[1.05rem] font-bold leading-relaxed">{d.verdict}</div>
          </section>
          <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="可以當沖" value={lists.go.length} valueClass={lists.go.length ? 'text-up' : ''} sub={`做多 ${d.counts['可以當沖做多']}・做空 ${d.counts['可以當沖做空']}`} />
            <Stat label="盤中訊號成立" value={lists.signal.length} sub="做多＋做空" />
            <Stat label="等突破再進" value={lists.wait.length} sub="站上觸發價再看" />
            <Stat label="今日可當沖標的" value={fmtInt(d.eligible_count)} sub={d.source} />
          </div>
          <Section title="當沖建議排行" aside="盤中每 15 秒更新" className="[&>div:last-child]:p-0">
            <Tabs<Tab> value={tab} onChange={setTab} tabs={[
              { key: 'all', label: '全部排行', count: lists.all.length },
              { key: 'go', label: '可以當沖', count: lists.go.length, tone: 'text-up' },
              { key: 'signal', label: '訊號成立', count: lists.signal.length },
              { key: 'wait', label: '等突破', count: lists.wait.length },
            ]} />
            <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-3 text-sm" role="group" aria-label="股價上限">
              <span className="mr-1">股價</span>
              {[0, 100, 200, 500].map((v) => (
                <button key={v} onClick={() => pickCap(v)}
                  className={`rounded-md px-3 py-1 ${cap === v ? 'bg-link text-white' : 'border border-line text-ink-2 hover:text-ink'}`}>
                  {v ? `${v} 元內` : '不限'}
                </button>
              ))}
              {d.max_price ? <span className="ml-1 text-muted">・依昨收篩選，{fmtInt(d.n_in_range)} 檔可當沖標的在範圍內，取當沖適合度前 {rows.length} 檔排名</span> : null}
            </div>
            <div className="p-4"><Rows rows={lists[tab]} /></div>
          </Section>
          <div className="grid items-start gap-x-6 lg:grid-cols-2">
            <Section title="查某檔能不能當沖"><Lookup /></Section>
            <Section title="操作參數">
              <Bullets items={d.rules} />
              <p className="mt-3 text-sm text-ink-2">{d.cost_note}</p>
              {d.capital_note && <p className="mt-1 text-sm text-ink-2">{d.capital_note}（設定 daytrade.capital 可改成你實際的當沖資金）</p>}
            </Section>
          </div>
          <Section title="回測參考（日 K 近似）">
            <Backtest bt={d.backtest} />
          </Section>
          <p className="text-sm leading-relaxed text-muted">{d.note}</p>
        </>
      )}
    </>
  )
}
