import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useChat } from '../components/Chat'
import { Change, Empty, ErrorNote, Loading, PageTitle, Section } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtPct } from '../lib/format'

const LEVEL_STYLE: Record<string, string> = {
  嚴重: 'border-up bg-up/5',
  警告: 'border-warn bg-warn/5',
  提醒: 'border-line bg-paper',
}
const LEVEL_TEXT: Record<string, string> = { 嚴重: 'text-up', 警告: 'text-warn', 提醒: 'text-muted' }

function AlertCard({ a }: { a: Json }) {
  const chat = useChat()
  return (
    <article className={`rounded-lg border-l-4 px-4 py-3 ${LEVEL_STYLE[a.level] ?? ''}`}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={`font-bold ${LEVEL_TEXT[a.level]}`}>{a.level}</span>
        <span className="text-sm text-muted">{a.scope}</span>
        {a.code ? (
          <Link to={`/stock/${a.code}`} className="font-bold text-ink no-underline hover:text-link">{a.title}</Link>
        ) : (
          <span className="font-bold">{a.title}</span>
        )}
        {a.price != null && (
          <span className="num text-sm">{a.price} <Change value={a.chg_pct} /></span>
        )}
      </div>
      <ul className="mt-1.5 list-disc space-y-1 pl-5 text-[0.95rem] leading-relaxed">
        {a.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}
      </ul>
      <p className="mt-2 font-medium">建議：{a.action}</p>
      {a.why_qty && <p className="text-sm text-ink-2">{a.why_qty}</p>}
      {a.news?.length > 0 && (
        <div className="mt-1.5 flex flex-col gap-0.5 text-sm">
          {a.news.map((n: Json, i: number) => (
            <a key={i} href={n.url} target="_blank" rel="noreferrer">{n.title}</a>
          ))}
        </div>
      )}
      {a.code && (
        <button onClick={() => chat.ask(`${a.code} ${a.name} 出現警示：${a.reasons[0]}。我該怎麼處理？`)} className="mt-2 text-sm text-link hover:underline">
          問 AI 助理怎麼處理
        </button>
      )}
    </article>
  )
}

function LimitTable({ rows, kind }: { rows: Json[]; kind: 'up' | 'down' }) {
  const [all, setAll] = useState(false)
  const shown = all ? rows : rows.slice(0, 12)
  return (
    <>
      <ul className="divide-y divide-line/60">
        {shown.map((q) => (
          <li key={q.code} className="py-2">
            <div className="flex flex-wrap items-baseline gap-x-3">
              <Link to={`/stock/${q.code}`} className="text-ink no-underline hover:text-link"><span className="num text-muted">{q.code}</span> {q.name}</Link>
              <span className={`num ${kind === 'up' ? 'text-up' : 'text-down'}`}>{fmtPct(q.chg_pct)}</span>
              <span className="text-xs text-muted">{q.industry ?? ''}</span>
            </div>
            {q.reason && <div className="text-sm text-ink-2">{q.reason}</div>}
            {q.news?.length > 0 && (
              <div className="flex flex-wrap gap-x-3 text-xs">
                {q.news.map((n: Json, i: number) => <a key={i} href={n.url} target="_blank" rel="noreferrer">{n.source || '新聞'}</a>)}
              </div>
            )}
          </li>
        ))}
      </ul>
      {rows.length > 12 && (
        <button onClick={() => setAll(!all)} className="mt-2 text-sm text-link">{all ? '收合' : `顯示全部 ${rows.length} 檔`}</button>
      )}
    </>
  )
}

export default function Alerts() {
  const qc = useQueryClient()
  const { data: d, isLoading, error, isFetching } = useQuery<Json>({ queryKey: ['alerts'], queryFn: () => get('/api/alerts'), refetchInterval: 300_000 })
  if (isLoading) return <Loading label="彙整警示中（首次需蒐集新聞，約 30 秒）" />
  if (error) return <ErrorNote error={error} />
  const groups = ['持股', '買進前', '大盤', '國際', '題材']
  return (
    <>
      <PageTitle
        aside={
          <span className="flex items-center gap-3 text-sm text-muted">
            {d.trading_now ? '盤中即時' : `收盤資料 ${d.quotes_date ?? ''}`}・更新 {d.generated_at}
            <button disabled={isFetching} onClick={() => get('/api/alerts?refresh=true').then((v) => qc.setQueryData(['alerts'], v))}
              className="rounded-md border border-line bg-paper px-2.5 py-1 text-ink disabled:opacity-50">重新整理</button>
          </span>
        }
      >
        警示提醒
      </PageTitle>

      <div className="mb-6 flex flex-wrap gap-x-8 gap-y-2 text-[0.95rem]">
        <span><b className="text-up">{d.counts['嚴重'] ?? 0}</b> 嚴重</span>
        <span><b className="text-warn">{d.counts['警告'] ?? 0}</b> 警告</span>
        <span><b>{d.counts['提醒'] ?? 0}</b> 提醒</span>
        <span className="text-ink-2">全市場漲停 <b className="text-up">{d.limit_up_count}</b>・跌停 <b className="text-down">{d.limit_down_count}</b></span>
        {d.taiex && <span className="text-ink-2">加權指數 <Change value={d.taiex.chg_pct} /></span>}
      </div>

      {!d.alerts.length && <Empty>目前持股與今日建議都沒有需要處理的警示。</Empty>}
      {groups.map((g) => {
        const items = d.alerts.filter((a: Json) => a.scope === g)
        if (!items.length) return null
        return (
          <Section key={g} title={g === '持股' ? '持股風險' : g === '買進前' ? '今日建議買進前注意' : g === '題材' ? '題材動向' : `${g}風險`}>
            <div className="space-y-3">{items.map((a: Json, i: number) => <AlertCard key={i} a={a} />)}</div>
          </Section>
        )
      })}

      <div className="grid gap-8 lg:grid-cols-2">
        <Section title={`跌停（${d.limit_down_count}）`} aside={d.down_industries?.slice(0, 3).map((g: Json) => `${g.industry} ${g.count}`).join('、')}>
          {d.limit_down.length ? <LimitTable rows={d.limit_down} kind="down" /> : <p className="text-muted">沒有跌停股。</p>}
        </Section>
        <Section title={`漲停（${d.limit_up_count}）`} aside={d.up_industries?.slice(0, 3).map((g: Json) => `${g.industry} ${g.count}`).join('、')}>
          {d.limit_up.length ? <LimitTable rows={d.limit_up} kind="up" /> : <p className="text-muted">沒有漲停股。</p>}
        </Section>
      </div>
      <p className="text-xs text-muted">
        漲跌停原因只列出實際新聞標題，未經 AI 改寫；查無新聞時以同產業連動判斷。持股與今日建議股票在盤中使用證交所即時報價，全市場漲跌停使用最近收盤資料。
        目前全市場注意股 {d.attention_total?.['注意'] ?? 0} 檔、處置股 {d.attention_total?.['處置'] ?? 0} 檔。
      </p>
    </>
  )
}
