import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Json } from '../lib/api'
import { fmtRatio } from '../lib/format'
import { useIntl, useQuotes } from '../lib/live'
import { useChat } from './Chat'
import { IntlNote, LivePrice } from './LiveBits'
import { BasketTag, Section } from './ui'

function Row({ x, q, notes }: { x: Json; q?: Json; notes?: Json[] }) {
  const { ask } = useChat()
  const metric = x.basket === 'etf'
    ? `${x.category}・收正機率 ${fmtRatio(x.p_up)}・預期回檔 ${fmtRatio(x.exp_dd, 1, true)}${x.premium_pct != null ? `・溢價 ${x.premium_pct > 0 ? '+' : ''}${x.premium_pct}%` : ''}`
    : x.basket === 'offense'
    ? `創新高機率 ${fmtRatio(x.p_newhigh)}・預期超額 ${fmtRatio(x.exp_excess, 1, true)}`
    : `收正機率 ${fmtRatio(x.p_up)}・預期回檔 ${fmtRatio(x.exp_dd, 1, true)}`
  return (
    <li className="py-2.5 text-[0.95rem]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <BasketTag basket={x.basket} />
        <span className={`rounded px-1.5 py-0.5 text-xs ${x.kind === '候補' ? 'bg-mist text-ink-2' : 'bg-warn/15 text-warn'}`}>{x.kind}</span>
        <Link to={`/stock/${x.code}`} className="font-medium text-ink no-underline hover:text-link"><span className="num text-muted">{x.code}</span> {x.name}</Link>
        <LivePrice q={q} />
        <span className="text-sm text-muted">{metric}</span>
        <IntlNote notes={notes} />
        <button type="button" onClick={() => ask(`${x.code} ${x.name} 現在可以買嗎？要買多少？`)} className="ml-auto text-sm">問 AI</button>
      </div>
      <div className="mt-0.5 text-sm text-ink-2">{x.why_not_yet}；{x.trigger}</div>
    </li>
  )
}

/** 潛在標的：模型看好但今天還沒列入下單（排名候補、過熱等回檔）。 */
export default function Potential({ asset = 'STOCK' }: { asset?: 'STOCK' | 'ETF' }) {
  const { data: d } = useQuery<Json>({ queryKey: ['potential'], queryFn: () => get('/api/potential'), refetchInterval: 600_000 })
  // 盤中計畫（每 40 分鐘重算）已經要買的不算潛在標的
  const { data: intra } = useQuery<Json>({ queryKey: ['intraday'], queryFn: () => get('/api/intraday'), staleTime: 60_000 })
  const planned = new Set<string>((intra?.orders ?? []).map((o: Json) => o.code))
  const baskets = (asset === 'ETF' ? ['etf'] : ['offense', 'defense']) as ('offense' | 'defense' | 'etf')[]
  const pick = (b: 'offense' | 'defense' | 'etf'): Json[] => (d?.[b] ?? []).filter((x: Json) => !planned.has(x.code))
  const list: Json[] = baskets.flatMap((b) => pick(b))
  const codes = list.map((x) => x.code)
  const { data: q } = useQuotes(codes)
  const { data: intl } = useIntl(codes)
  if (!list.length) return null
  const quotes: Json = q?.quotes ?? {}
  const notes: Json = intl?.impacts ?? {}
  return (
    <Section title="潛在標的" aside={<span className="text-sm text-muted">模型看好、今天還沒列入下單</span>}>
      <div className={`grid gap-x-8 ${baskets.length > 1 ? 'md:grid-cols-2' : ''}`}>
        {baskets.map((b) => (
          <div key={b}>
            <h3 className={`mb-1 font-medium ${b === 'offense' ? 'text-offense' : b === 'etf' ? 'text-etf' : 'text-defense'}`}>{b === 'offense' ? '進攻籃' : b === 'etf' ? 'ETF 籃' : '穩健籃'}</h3>
            <ul className="divide-y divide-line/60">
              {pick(b).map((x: Json) => <Row key={x.code} x={x} q={quotes[x.code]} notes={notes[x.code]} />)}
            </ul>
          </div>
        ))}
      </div>
      <p className="mt-2 text-xs text-muted">{d?.note}</p>
    </Section>
  )
}
