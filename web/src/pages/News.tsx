import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Empty, ErrorNote, Loading, PageTitle, Section } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtPct, tone } from '../lib/format'

function Sentiment({ v }: { v: number }) {
  const label = v >= 2 ? '強烈利多' : v === 1 ? '利多' : v === 0 ? '中性' : v === -1 ? '利空' : '強烈利空'
  return <span className={`shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${v > 0 ? 'bg-up/10 text-up' : v < 0 ? 'bg-down/10 text-down' : 'bg-mist text-muted'}`}>{label}</span>
}

export default function News() {
  const { data, isLoading, error } = useQuery<Json>({ queryKey: ['news'], queryFn: () => get('/api/news') })
  const [src, setSrc] = useState('')
  const [q, setQ] = useState('')
  const sources = useMemo(() => [...new Set<string>((data?.news ?? []).map((n: Json) => n.source.split(':')[0]))], [data])
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  const news = (data.news as Json[]).filter((n) => (!src || n.source.startsWith(src)) && (!q || n.title.includes(q)))
  return (
    <>
      <PageTitle aside={<span className="text-sm text-muted">{data.date} 盤前蒐集</span>}>新聞與國際大事</PageTitle>
      {data.market_view && <p className="title mb-8 max-w-3xl text-lg leading-relaxed">{data.market_view}</p>}

      <Section title="國際大事對持股的影響" aside="影響金額依各股歷史敏感度計算">
        {!data.macro?.length && <Empty>沒有辨識出重大國際事件。</Empty>}
        <div className="space-y-5">
          {data.macro?.map((e: Json, i: number) => (
            <article key={i}>
              <h3 className="font-bold leading-snug">{e.event}</h3>
              <p className="text-sm text-ink-2">
                {e.factor_name}{e.direction > 0 ? '上升' : '下降'}・{e.horizon}・合計預估 <span className={`num ${tone(e.total_amount)}`}>{fmtInt(e.total_amount)} 元</span>（{fmtPct(e.total_pct_nav)}）
              </p>
              {e.rows?.length > 0 && (
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full min-w-[560px] text-sm">
                    <tbody>
                      {e.rows.map((r: Json) => (
                        <tr key={r.code} className="border-b border-line/60">
                          <td className="py-1.5 pr-3"><Link to={`/stock/${r.code}`} className="text-ink no-underline hover:text-link">{r.name}</Link></td>
                          <td className={`num pr-3 text-right ${tone(r.impact_pct)}`}>{fmtPct(r.impact_pct)}</td>
                          <td className={`num pr-3 text-right ${tone(r.amount)}`}>{fmtInt(r.amount)} 元</td>
                          <td className="text-ink-2">{r.decision}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="mt-1 flex flex-wrap gap-x-4 text-sm">
                {e.news.slice(0, 3).map((n: Json) => <a key={n.id} href={n.url} target="_blank" rel="noreferrer">{n.title}</a>)}
              </div>
            </article>
          ))}
        </div>
      </Section>

      <Section title="個股事件">
        {!data.events?.length && <Empty>沒有個股事件。</Empty>}
        <ul className="space-y-3">
          {data.events?.map((e: Json, i: number) => (
            <li key={i} className="flex gap-3">
              <Sentiment v={e.sentiment} />
              <div className="min-w-0">
                <div className="leading-snug">
                  {e.codes.filter((c: string) => c !== 'MARKET').map((c: string) => <Link key={c} to={`/stock/${c}`} className="num mr-1.5">{c}</Link>)}
                  {e.event}{e.multi_source && <span className="ml-1.5 text-xs text-muted">多來源</span>}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-4 text-sm">
                  {e.news.slice(0, 3).map((n: Json) => <a key={n.id} href={n.url} target="_blank" rel="noreferrer">{n.title}</a>)}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </Section>

      <Section title={`全部新聞（${news.length}）`}
        aside={
          <span className="flex gap-2">
            <label className="sr-only" htmlFor="news-src">來源</label>
            <select id="news-src" value={src} onChange={(e) => setSrc(e.target.value)} className="rounded-md border border-line bg-paper px-2 py-1 text-ink">
              <option value="">全部來源</option>
              {sources.map((s) => <option key={s}>{s}</option>)}
            </select>
            <label className="sr-only" htmlFor="news-q">關鍵字</label>
            <input id="news-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="關鍵字" className="w-28 rounded-md border border-line bg-paper px-2 py-1 text-ink" />
          </span>
        }>
        <ul className="divide-y divide-line/60">
          {news.map((n) => (
            <li key={n.id} className="flex gap-3 py-2">
              <span className="num w-20 shrink-0 text-sm text-muted">{n.time}</span>
              <div className="min-w-0">
                <a href={n.url} target="_blank" rel="noreferrer" className="leading-snug">{n.title}</a>
                <div className="text-xs text-muted">{n.source}</div>
              </div>
            </li>
          ))}
        </ul>
      </Section>
    </>
  )
}
