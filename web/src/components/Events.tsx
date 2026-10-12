import { useQuery } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { Change, Empty, Loading, Section, StockLink } from './ui'
import { get, type Json } from '../lib/api'
import { fmtInt } from '../lib/format'
import { liveEvery } from '../lib/live'

const TIER_STYLE: Record<string, string> = { A: 'bg-up text-white', L: 'bg-warn text-white', B: 'bg-mist text-ink-2' }

/** 資訊事件：今天因消息爆量跳空大漲的股票（法說會、營收、重大訊息），附新聞標題與歷史同類事件報酬。 */
export default function Events() {
  const { data: d, isLoading } = useQuery<Json>({ queryKey: ['events'], queryFn: () => get('/api/events'), refetchInterval: liveEvery(30_000, 600_000) })
  const [open, setOpen] = useState<string | null>(null)
  return (
    <Section title="資訊事件：今天因消息大漲" aside={d ? `${d.trading ? '盤中每 30 秒' : '收盤後'}・${d.time.slice(11, 16)}` : undefined} className="[&>div:last-child]:p-0">
      <div className="p-4">
        {isLoading && <Loading label="掃描全市場（約 30 秒）" />}
        {d && !d.items.length && <Empty>今天沒有符合條件的資訊事件</Empty>}
        {d?.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-[0.95rem]">
              <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
                <th className="py-2 font-normal">分級</th><th className="font-normal">股票</th><th className="text-right font-normal">現價</th>
                <th className="text-right font-normal">漲跌</th><th className="text-right font-normal">開盤跳空</th><th className="text-right font-normal">量比</th>
                <th className="text-right font-normal">一張</th><th className="pl-4 font-normal">可能原因</th>
              </tr></thead>
              <tbody>
                {d.items.map((r: Json) => (
                  <Fragment key={r.code}>
                    <tr className="cursor-pointer border-b border-line/60 align-top hover:bg-mist" onClick={() => setOpen(open === r.code ? null : r.code)}>
                      <td className="py-2"><span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${TIER_STYLE[r.tier]}`}>{r.label}</span></td>
                      <td className="py-2"><StockLink code={r.code} name={r.name} /></td>
                      <td className="num py-2 text-right">{r.price}</td>
                      <td className="py-2 text-right"><Change value={r.chg_pct} /></td>
                      <td className="num py-2 text-right">{r.gap_pct > 0 ? '+' : ''}{r.gap_pct}%</td>
                      <td className="num py-2 text-right">{r.volume_ratio}</td>
                      <td className="num py-2 text-right text-sm">{fmtInt(r.lot_cost)}</td>
                      <td className="max-w-[18rem] truncate py-2 pl-4 text-sm text-ink-2">{r.news?.[0]?.title ?? '—'}</td>
                    </tr>
                    {open === r.code && (
                      <tr className="border-b border-line/60 bg-mist/60">
                        <td />
                        <td colSpan={7} className="py-2 pr-2 text-sm leading-relaxed text-ink-2">
                          <div className="font-medium text-ink">{r.verdict}：{r.plan}</div>
                          <div className="mt-0.5">{r.stats}</div>
                          {r.news?.length > 0 && (
                            <ul className="mt-1 list-disc space-y-0.5 pl-4">
                              {r.news.map((n: Json, i: number) => (
                                <li key={i}>{n.url ? <a href={n.url} target="_blank" rel="noreferrer">{n.title}</a> : n.title}<span className="ml-1 text-xs text-muted">{n.source} {n.date}</span></li>
                              ))}
                            </ul>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {d && <p className="mt-2 text-xs leading-relaxed text-muted">{d.note}</p>}
      </div>
    </Section>
  )
}
