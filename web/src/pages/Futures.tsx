import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { useChat } from '../components/Chat'
import Derivatives from '../components/Derivatives'
import KLine from '../components/KLine'
import { Empty, ErrorNote, Loading, PageTitle, Panel, Section, Stat } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtInt, tone } from '../lib/format'

const ROOTS = [
  { code: 'TX', label: '大台' },
  { code: 'MTX', label: '小台' },
  { code: 'TMF', label: '微台' },
  { code: 'TXO', label: '台指選擇權' },
]

function PutTable({ d }: { d: Json }) {
  return (
    <>
      {d.months.map((m: Json) => (
        <Section key={m.ym} title={`${m.ym.slice(0, 4)}/${m.ym.slice(4)} 月賣權`} aside={`結算 ${m.settlement}・剩 ${m.days_left} 天`}>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-[0.95rem]">
              <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
                <th className="py-2 font-normal">履約價</th><th className="font-normal">距現價</th><th className="text-right font-normal">買價</th>
                <th className="text-right font-normal">賣價</th><th className="text-right font-normal">成交</th><th className="text-right font-normal">買 1 口權利金</th>
              </tr></thead>
              <tbody>
                {[...(d.puts[m.ym] ?? [])].reverse().map((r: Json) => {
                  const atm = d.spot && Math.abs(r.strike - d.spot) < 100
                  return (
                    <tr key={r.contract} className={`border-b border-line/60 ${atm ? 'bg-mist' : ''}`}>
                      <td className="num py-1.5">{fmtInt(r.strike)}</td>
                      <td className="text-sm text-ink-2">{r.dist_pct == null ? '—' : r.dist_pct < 0 ? `價外 ${Math.abs(r.dist_pct).toFixed(1)}%` : `價內 ${r.dist_pct.toFixed(1)}%`}</td>
                      <td className="num text-right">{r.bid ?? '—'}</td>
                      <td className="num text-right">{r.ask ?? '—'}</td>
                      <td className="num text-right">{r.last ?? '—'}</td>
                      <td className="num text-right">{r.premium_per_lot ? fmtInt(r.premium_per_lot) : '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Section>
      ))}
      <p className="text-sm text-ink-2">{d.note}。台指選擇權每點 50 元；比賽禁止對流動性差的標的異常交易，系統只建議有買賣報價、價差 15% 以內的履約價。</p>
    </>
  )
}

export default function Futures() {
  const { root = 'TX' } = useParams()
  const code = root.toUpperCase()
  const chat = useChat()
  const { data: d, isLoading, error } = useQuery<Json>({
    queryKey: ['futures', code], queryFn: () => get(`/api/futures/${code}`),
    refetchInterval: (q) => {
      const n = new Date()
      const hm = n.getHours() * 60 + n.getMinutes()
      return q.state.data && n.getDay() >= 1 && n.getDay() <= 5 && hm >= 525 && hm <= 825 ? 10_000 : 60_000
    },
  })
  const front = d?.months?.[0]
  return (
    <>
      <PageTitle aside={<button onClick={() => chat.ask(code === 'TXO' ? '現在需要買賣權避險嗎？' : `${d?.name ?? code} 現在可以做嗎？`)} className="rounded-md bg-ink px-4 py-2 text-sm text-paper">問 AI</button>}>
        期貨與選擇權
      </PageTitle>
      <nav className="mb-6 flex flex-wrap gap-2" aria-label="商品">
        {ROOTS.map((r) => (
          <Link key={r.code} to={`/futures/${r.code}`}
            className={`rounded-full border px-4 py-1.5 text-sm no-underline ${r.code === code ? 'border-fut bg-fut text-white' : 'border-line bg-paper text-ink'}`}>
            {r.label}<span className="num ml-1 opacity-70">{r.code}</span>
          </Link>
        ))}
      </nav>
      {isLoading && <Loading />}
      {error && <ErrorNote error={error} />}
      {d && (
        <>
          <div className="mb-6 flex flex-wrap items-baseline gap-x-8 gap-y-3">
            <div>
              <div className="text-sm text-muted">{d.name}{front ? `・${front.contract}` : ''}</div>
              {code === 'TXO'
                ? <div className="num text-4xl font-bold">{d.spot ? fmtInt(d.spot) : '—'}<span className="ml-2 text-base font-normal text-muted">加權現貨</span></div>
                : <div className={`num text-4xl font-bold ${tone(front?.chg_pct)}`}>{front?.price ? fmtInt(front.price) : '—'}
                    {front?.chg_pct != null && <span className="ml-2 text-lg">{front.chg_pct > 0 ? '+' : ''}{front.chg_pct}%</span>}</div>}
            </div>
            {code !== 'TXO' && (
              <>
                <Stat label="加權現貨" value={d.spot ? fmtInt(d.spot) : '—'} sub={front?.basis != null ? `基差 ${front.basis > 0 ? '+' : ''}${front.basis}` : undefined} />
                <Stat label="1 口契約價值" value={d.notional ? `${fmtInt(d.notional)} 元` : '—'} sub={`每點 ${d.multiplier} 元`} />
                <Stat label="原始／維持保證金" value={d.margin?.initial ? `${fmtInt(d.margin.initial)}` : '—'} sub={d.margin?.maintenance ? `維持 ${fmtInt(d.margin.maintenance)}・槓桿 ${d.leverage} 倍` : undefined} />
                <Stat label="結算日" value={front?.settlement ?? '—'} sub={front ? `剩 ${front.days_left} 天（第三個星期三）` : undefined} />
              </>
            )}
          </div>
          <Panel className="mb-6"><KLine code={code} url={`/api/futures/${code}/kline?days=260`} height={400} unit="口" futures /></Panel>
          <p className="-mt-4 mb-6 text-xs text-muted">K 線：期交所每日行情，近月合約連續（每天取成交量最大的月合約）{code === 'TXO' ? '；選擇權以台指期近月代表標的走勢' : ''}。報價：{d.source ?? '期交所'}，盤中每 10 秒更新。</p>

          {d.system_view ? <div className="mb-8"><Derivatives d={d.system_view} /></div>
            : <Empty>系統的期貨建議計算中（首頁盤中建議算完後會出現）。</Empty>}

          {code === 'TXO' ? <PutTable d={d} /> : (
            <Section title="各月合約">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-[0.95rem]">
                  <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
                    <th className="py-2 font-normal">合約</th><th className="text-right font-normal">價格</th><th className="text-right font-normal">漲跌</th>
                    <th className="text-right font-normal">基差</th><th className="text-right font-normal">結算日</th><th className="text-right font-normal">剩餘天數</th>
                  </tr></thead>
                  <tbody>
                    {d.months.map((m: Json) => (
                      <tr key={m.contract} className="border-b border-line/60">
                        <td className="py-1.5">{m.contract}</td>
                        <td className="num text-right">{fmtInt(m.price)}</td>
                        <td className={`num text-right ${tone(m.chg_pct)}`}>{m.chg_pct != null ? `${m.chg_pct > 0 ? '+' : ''}${m.chg_pct}%` : '—'}</td>
                        <td className="num text-right">{m.basis ?? '—'}</td>
                        <td className="num text-right">{m.settlement}</td>
                        <td className="num text-right">{m.days_left}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-2 text-sm text-ink-2">交易時間 {d.trading_hours}；{d.tax}；手續費以每口 {d.commission_assumed} 元估算（官網未列，依平台實際為準）。期貨不計入持股率（官網未明說，先保守處理）。</p>
            </Section>
          )}
        </>
      )}
    </>
  )
}
