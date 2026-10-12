import { useQuery } from '@tanstack/react-query'
import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Change, Empty, ErrorNote, Loading, PageTitle, Section, Tabs } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtPct, tone } from '../lib/format'
import { liveEvery } from '../lib/live'

function Sentiment({ v }: { v: number }) {
  const label = v >= 2 ? '強烈利多' : v === 1 ? '利多' : v === 0 ? '中性' : v === -1 ? '利空' : '強烈利空'
  return <span className={`shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${v > 0 ? 'bg-up/10 text-up' : v < 0 ? 'bg-down/10 text-down' : 'bg-mist text-muted'}`}>{label}</span>
}

export default function News() {
  const { data, isLoading, error } = useQuery<Json>({ queryKey: ['news'], queryFn: () => get('/api/news') })
  // 即時新聞：進入或重新整理頁面時立即抓取，之後每 5 分鐘更新
  const first = useRef(true)
  const { data: live, isFetching: liveFetching, refetch } = useQuery<Json>({
    queryKey: ['news-live'],
    queryFn: () => {
      const r = get(`/api/news/live?refresh=${first.current}`)
      first.current = false
      return r
    },
    refetchInterval: liveEvery(120_000, 300_000),
    staleTime: 0,
  })
  const [src, setSrc] = useState('')
  const [cat, setCat] = useState('')
  const [q, setQ] = useState('')
  const [shown, setShown] = useState(30)  // 一次顯示 30 則，按「看更多」再載入
  const allNews: Json[] = useMemo(() => live?.news ?? data?.news ?? [], [live, data])
  const sources = useMemo(() => [...new Set<string>(allNews.map((n: Json) => n.source.split(':')[0]))], [allNews])
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  const news = allNews.filter((n) => (!src || n.source.startsWith(src)) && (!q || n.title.includes(q)) && (!cat || n.category === cat))
  const count = (c: string) => allNews.filter((n) => n.category === c).length
  const forceRefresh = () => {
    first.current = true
    refetch()
  }
  return (
    <>
      <PageTitle aside={<span className="text-xs text-muted">{data.date} 盤前蒐集</span>}>新聞與國際大事</PageTitle>
      {data.market_view && (
        <section className="mb-6 rounded-xl border border-line border-l-4 border-l-link bg-paper px-5 py-4 shadow-sm">
          <div className="mb-1 text-xs font-medium text-muted">今日市場觀點</div>
          <p className="text-[1.05rem] font-medium leading-relaxed">{data.market_view}</p>
        </section>
      )}

      <div className="grid items-start gap-6 lg:grid-cols-3">
      <div className="min-w-0 lg:col-span-2">
      <Section title={`最新新聞（${news.length}）`}
        aside={
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-xs">{live?.fetched_at ? `更新於 ${live.fetched_at}・每 5 分鐘` : ""}</span>
            <button onClick={forceRefresh} disabled={liveFetching} className="rounded-md border border-line bg-paper px-2 py-1 text-ink disabled:opacity-50">{liveFetching ? "更新中" : "立即更新"}</button>
            <label className="sr-only" htmlFor="news-src">來源</label>
            <select id="news-src" value={src} onChange={(e) => setSrc(e.target.value)} className="rounded-md border border-line bg-paper px-2 py-1 text-ink">
              <option value="">全部來源</option>
              {sources.map((s) => <option key={s}>{s}</option>)}
            </select>
            <label className="sr-only" htmlFor="news-q">關鍵字</label>
            <input id="news-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="關鍵字" className="w-28 rounded-md border border-line bg-paper px-2 py-1 text-ink" />
          </span>
        }>
        <div className="-mx-4 -mt-4 mb-1">
          <Tabs value={cat} onChange={(v) => { setCat(v); setShown(30) }}
            tabs={[{ key: '', label: '全部', count: allNews.length }, { key: '股票', label: '股票', count: count('股票') },
              { key: 'ETF', label: 'ETF', count: count('ETF') }, { key: '期貨', label: '期貨與選擇權', count: count('期貨') }]} />
        </div>
        <ul className="divide-y divide-line/60">
          {news.slice(0, shown).map((n) => (
            <li key={n.id} className="flex gap-3 py-2.5">
              <span className="num w-20 shrink-0 pt-0.5 text-xs text-muted">{n.time}</span>
              <div className="min-w-0">
                <a href={n.url} target="_blank" rel="noreferrer" className="font-medium leading-snug text-ink no-underline hover:text-link">{n.title}</a>
                <div className="mt-0.5 text-xs text-muted">{n.category && <span className="mr-2 rounded bg-mist px-1.5 py-0.5">{n.category === '期貨' ? '期貨與選擇權' : n.category}</span>}{n.source}</div>
              </div>
            </li>
          ))}
        </ul>
        {news.length > shown && (
          <button onClick={() => setShown(shown + 30)} className="mt-3 w-full rounded-md border border-line py-2 text-sm text-ink hover:border-link">
            看更多（還有 {news.length - shown} 則）
          </button>
        )}
      </Section>
      </div>

      <div className="min-w-0">
      {(data.us_snapshot?.length > 0 || data.market_summary) && (
        <Section title="國際市場" aside={data.us_snapshot?.[0]?.date ? `${data.us_snapshot[0].date} 收盤` : undefined}>
          {data.market_summary && <p className="mb-3 text-sm leading-relaxed text-ink-2">{data.market_summary}</p>}
          <div className="text-[0.95rem]">
            {data.us_snapshot.map((u: Json) => (
              <div key={u.ticker} className="flex justify-between border-b border-line/60 py-1.5 last:border-0">
                <span className="truncate pr-2">{u.name}</span>
                <Change value={u.change_pct} />
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section title="個股事件">
        {!data.events?.length && <Empty>沒有個股事件。</Empty>}
        <ul className="space-y-3">
          {data.events?.map((e: Json, i: number) => (
            <li key={i} className="flex gap-2.5">
              <Sentiment v={e.sentiment} />
              <div className="min-w-0 text-sm">
                <div className="leading-snug">
                  {e.codes.filter((c: string) => c !== 'MARKET').map((c: string) => <Link key={c} to={`/stock/${c}`} className="num mr-1.5">{c}</Link>)}
                  {e.event}{e.multi_source && <span className="ml-1.5 text-xs text-muted">多來源</span>}
                </div>
                <div className="mt-0.5 flex flex-col gap-0.5 text-xs">
                  {e.news.slice(0, 2).map((n: Json) => <a key={n.id} href={n.url} target="_blank" rel="noreferrer" className="truncate">{n.title}</a>)}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </Section>
      </div>
      </div>

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


    </>
  )
}
