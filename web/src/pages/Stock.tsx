import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { useChat } from '../components/Chat'
import Company from '../components/Company'
import KLine from '../components/KLine'
import { BasketTag, Bullets, Change, Empty, ErrorNote, Loading, PageTitle, Panel, Section, Stat } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtNum, fmtPct, fmtRatio, qty, tone } from '../lib/format'

function Profile({ p }: { p: Json }) {
  const v = p.valuation || {}
  const rev = p.revenue || {}
  const inst = p.institutional_5d_lots
  const m = p.margin || {}
  const rows: [string, string, string?][] = [
    ['52 週高／低', `${fmtNum(p.high52)}／${fmtNum(p.low52)}`, `距高點 ${fmtPct(p.dist_high52_pct, 1)}`],
    ['近 20／60 日', `${fmtPct(p.ret20_pct, 1)}／${fmtPct(p.ret60_pct, 1)}`],
    ['成交量', `${fmtInt(p.volume_lots)} 張`, `20 日均量 ${fmtInt(p.avg_volume20_lots)} 張`],
    ['本益比／淨值比', v.per != null ? `${fmtNum(v.per)}／${fmtNum(v.pbr)}` : '—'],
    ['殖利率', v.dividend_yield != null ? `${fmtNum(v.dividend_yield)}%` : '—'],
    ['月營收', rev.month ? `${rev.month} ${rev.revenue_100m} 億` : '—', rev.yoy != null ? `年增 ${fmtRatio(rev.yoy, 1, true)}、月增 ${fmtRatio(rev.mom, 1, true)}` : undefined],
    ['法人近 5 日', inst ? `${inst.total > 0 ? '買超' : '賣超'} ${fmtInt(Math.abs(inst.total))} 張` : '—', inst ? `外資 ${fmtInt(inst.foreign)}、投信 ${fmtInt(inst.trust)}、自營 ${fmtInt(inst.dealer)}` : undefined],
    ['融資／融券餘額', m.margin_balance != null ? `${fmtInt(m.margin_balance)}／${fmtInt(m.short_balance)} 張` : '—', m.margin_chg5 != null ? `5 日 融資 ${m.margin_chg5 > 0 ? '+' : ''}${fmtInt(m.margin_chg5)}、融券 ${m.short_chg5 > 0 ? '+' : ''}${fmtInt(m.short_chg5)}` : undefined],
  ]
  return (
    <dl className="grid grid-cols-1 gap-x-8 sm:grid-cols-2">
      {rows.map(([k, val, sub]) => (
        <div key={k} className="flex items-baseline justify-between gap-3 border-b border-line/70 py-2">
          <dt className="shrink-0 text-muted">{k}</dt>
          <dd className="num text-right">{val}{sub && <div className="text-xs text-muted">{sub}</div>}</dd>
        </div>
      ))}
    </dl>
  )
}

function Analysis({ code }: { code: string }) {
  const { data: a, isLoading, error } = useQuery<Json>({ queryKey: ['analysis', code], queryFn: () => get(`/api/stock/${code}/analysis`), staleTime: 300_000 })
  if (isLoading) return <Loading label="即時分析中（首次約 1 分鐘）" />
  if (error) return <ErrorNote error={error} />
  if (a.error) return <Empty>{a.error}</Empty>
  const plan = a.buy_plan
  const att = a.attribution
  return (
    <>
      <Section title="操作建議" aside={`資料至 ${a.data_date}`}>
        <p className="title mb-3 text-xl leading-snug">{a.verdict}</p>
        {a.warning && <p className="mb-3 rounded-md bg-warn/10 px-3 py-2 text-sm text-warn">{a.warning}</p>}
        {a.holding && (
          <p className="num mb-3 text-ink-2">持有 {fmtInt(a.holding.shares)} 股，成本 {a.holding.avg_cost}，未實現 <span className={tone(a.holding.pnl)}>{fmtInt(a.holding.pnl)}</span> 元，停損 {a.holding.stop ?? '—'}（{a.holding.basket}）</p>
        )}
        {a.recovery && <Bullets items={a.recovery.lines} />}
        {plan && plan.shares > 0 && (
          <Panel className="mt-2">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <BasketTag basket={plan.basket} />
              <span className="num text-lg">若買進：{qty(plan.lots, plan.odd)}　限價 <b>{plan.limit_price}</b>　停損 <b className="text-down">{plan.stop}</b></span>
              <span className="num text-sm text-muted">約 {fmtInt(plan.amount)} 元（{fmtPct(plan.weight_pct, 1, false)}）</span>
            </div>
            <div className="grid gap-6 md:grid-cols-2">
              <div><h3 className="mb-1 font-medium">張數怎麼算</h3><Bullets items={plan.why_qty} /></div>
              <div>
                <h3 className="mb-1 font-medium">出場計畫</h3><Bullets items={plan.exit_plan} />
                {(plan.why_more.length > 0 || plan.why_less.length > 0) && (
                  <><h3 className="mb-1 mt-3 font-medium">多買／少買</h3>
                    <Bullets items={[...plan.why_more.map((x: string) => `多買：${x}`), ...plan.why_less.map((x: string) => `少買：${x}`)]} /></>
                )}
              </div>
            </div>
          </Panel>
        )}
        {plan && plan.shares === 0 && <Bullets items={plan.notes} />}
      </Section>

      <div className="grid gap-8 md:grid-cols-2">
        <Section title={<span className="text-offense">進攻籃評估</span>} aside={`分數 ${a.offense.score >= 0 ? '+' : ''}${a.offense.score.toFixed(2)}・${a.offense.signal}`}>
          <Bullets items={a.offense.reasons} />
          {a.offense.risks.length > 0 && <div className="mt-2 text-ink-2"><Bullets items={a.offense.risks.map((x: string) => `風險：${x}`)} /></div>}
        </Section>
        <Section title={<span className="text-defense">穩健籃評估</span>} aside={`分數 ${a.defense.score >= 0 ? '+' : ''}${a.defense.score.toFixed(2)}・${a.defense.signal}`}>
          <Bullets items={a.defense.reasons.length ? a.defense.reasons : ['不在穩健籃資格內（過熱、離月線太遠或 β 偏高）']} />
          {a.defense.risks.length > 0 && <div className="mt-2 text-ink-2"><Bullets items={a.defense.risks.map((x: string) => `風險：${x}`)} /></div>}
        </Section>
      </div>

      {att && (
        <Section title={`${att.date} 為何${att.ret_pct >= 0 ? '漲' : '跌'}`} aside={<Change value={att.ret_pct} />}>
          <p className="mb-2">{att.verdict}</p>
          <Bullets items={att.explanations} />
        </Section>
      )}

      {a.macro_sensitivity?.length > 0 && (
        <Section title="國際因子敏感度" aside="因子變動 1 個標準差時的歷史平均影響">
          <div className="grid grid-cols-2 gap-x-6 sm:grid-cols-3">
            {a.macro_sensitivity.map((m: Json) => (
              <div key={m.factor} className="flex justify-between border-b border-line/60 py-1.5 text-[0.95rem]">
                <span className={m.significant ? '' : 'text-muted'}>{m.factor}</span>
                <span className={`num ${m.significant ? tone(m.impact_1sd_pct) : 'text-muted'}`}>{m.significant ? fmtPct(m.impact_1sd_pct) : '不顯著'}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section title="相關新聞">
        {a.news?.length ? (
          <ul className="space-y-2">
            {a.news.map((n: Json, i: number) => (
              <li key={i} className="leading-snug">
                <a href={n.url} target="_blank" rel="noreferrer" className="no-underline hover:underline">{n.title}</a>
                <div className="text-xs text-muted">{n.source} {n.time}</div>
              </li>
            ))}
          </ul>
        ) : <p className="text-muted">近 7 日沒有找到相關新聞。</p>}
      </Section>
    </>
  )
}

function EtfInfo({ code }: { code: string }) {
  const { data: a } = useQuery<Json>({ queryKey: ['analysis', code], queryFn: () => get(`/api/stock/${code}/analysis`), staleTime: 300_000 })
  const e = a?.etf
  if (!e) return <p className="mb-6 text-sm text-muted">ETF 資訊計算中</p>
  const pr = e.premium
  return (
    <Section title="ETF 資訊" aside={<BasketTag basket="etf" />}>
      <div className="mb-3 grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-5">
        <Stat label="類別" value={e.category ?? '—'} />
        <Stat label="預估淨值" value={pr ? pr.nav : '—'} sub={pr ? `市價 ${pr.price}` : '無法取得'} />
        <Stat label="折溢價" value={pr ? `${pr.premium_pct > 0 ? '+' : ''}${pr.premium_pct}%` : '—'}
          valueClass={pr && Math.abs(pr.premium_pct) >= 1 ? 'text-warn' : ''} sub={pr ? (pr.premium_pct > 1 ? '溢價過高不追' : '正常範圍') : undefined} />
        <Stat label="ETF 分數" value={e.score != null ? (e.score > 0 ? '+' : '') + e.score.toFixed(2) : '—'} sub={e.signal} />
        <Stat label="今日 ETF 籃比例" value={e.basket_share_today != null ? `${Math.round(e.basket_share_today * 100)}%` : '—'} sub="佔股票＋ETF 目標持股" />
      </div>
      <ul className="list-disc space-y-0.5 pl-5 text-[0.95rem] text-ink-2">
        {[...(e.reasons ?? []), ...(e.risks ?? [])].map((r: string, i: number) => <li key={i}>{r}</li>)}
      </ul>
      {pr?.time && <p className="mt-2 text-xs text-muted">折溢價資料：證交所 ETF 預估淨值（{pr.time}）</p>}
    </Section>
  )
}

export default function Stock() {
  const { code } = useParams()
  const chat = useChat()
  const { data: p, isLoading, error } = useQuery<Json>({ queryKey: ['profile', code], queryFn: () => get(`/api/stock/${code}/profile`), enabled: !!code })
  const { data: latest } = useQuery<Json>({ queryKey: ['report', 'latest'], queryFn: () => get('/api/report'), enabled: !code })
  if (!code)
    return (
      <>
        <PageTitle>個股查詢</PageTitle>
        <p className="mb-6 text-ink-2">用左側搜尋框輸入代號或名稱，任何上市櫃股票與 ETF 都能分析，不限每日建議名單。</p>
        {latest?.top_picks && (
          <Section title="一個月創新高潛力前段班">
            <div className="flex flex-wrap gap-2">
              {latest.top_picks.slice(0, 15).map((t: Json) => (
                <Link key={t.code} to={`/stock/${t.code}`} className="rounded-md border border-line bg-paper px-3 py-1.5 text-ink no-underline hover:border-link">
                  {t.name} <span className="num text-sm text-muted">{fmtRatio(t.p_newhigh, 0)}</span>
                </Link>
              ))}
            </div>
          </Section>
        )}
      </>
    )
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (p.error) return <Empty>{p.error}</Empty>
  return (
    <>
      <PageTitle aside={<button onClick={() => chat.ask(`${code} ${p.name} 現在可以買嗎？請給我完整建議`)} className="rounded-md bg-ink px-4 py-2 text-sm text-paper">問 AI 這檔</button>}>
        <span className="num text-muted">{p.code}</span> {p.name}
        <span className="ml-3 align-middle text-base font-normal text-muted">{p.sector}・{p.market === 'TPEx' ? '上櫃' : '上市'}{p.is_etf ? '・ETF' : ''}</span>
      </PageTitle>
      <div className="mb-6 flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <span className={`num text-4xl font-bold ${tone(p.change)}`}>{p.close}</span>
        <span className="text-lg"><Change value={p.change_pct} /></span>
        <span className="text-sm text-muted">{p.date} 收盤</span>
        {p.model && (
          <span className="flex gap-6">
            <Stat label="一個月創新高機率" value={fmtRatio(p.model.p_newhigh)} />
            <Stat label="一個月收正機率" value={fmtRatio(p.model.p_up)} />
            <Stat label="預期最大回檔" value={fmtRatio(p.model.exp_dd)} />
          </span>
        )}
      </div>
      <Panel className="mb-8"><KLine code={code} height={420} /></Panel>
      {p.is_etf && <EtfInfo code={code} />}
      <Section title="基本盤"><Profile p={p} /></Section>
      {!p.is_etf && <Company code={code} />}
      <Analysis code={code} />
    </>
  )
}
