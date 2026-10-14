import { useQuery } from '@tanstack/react-query'
import { get, type Json } from '../lib/api'
import { fmtInt, tone } from '../lib/format'
import { Section } from './ui'

/** 影子組合（完全照策略、開盤成交）vs 實際帳戶，差距拆解；今天的開盤預掛限價；成交時機最差的幾筆。 */
export default function ShadowCard() {
  const { data: s } = useQuery<Json>({ queryKey: ['shadow'], queryFn: () => get('/api/shadow'), staleTime: 30 * 60_000 })
  const { data: ex } = useQuery<Json>({ queryKey: ['execution'], queryFn: () => get('/api/execution'), staleTime: 10 * 60_000 })
  if (!s?.series) return null
  const last = s.series[s.series.length - 1]
  return (
    <Section title="影子組合與成交品質" aside={s.time}>
      <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-3">
        <div className="rounded-lg border border-line p-3"><div className="text-sm text-muted">實際帳戶</div>
          <div className={`num text-xl font-bold ${tone(last.actual_ret)}`}>{last.actual_ret > 0 ? '+' : ''}{last.actual_ret}%</div></div>
        <div className="rounded-lg border border-line p-3"><div className="text-sm text-muted">影子（照策略、開盤成交）</div>
          <div className={`num text-xl font-bold ${tone(last.shadow_ret)}`}>{last.shadow_ret > 0 ? '+' : ''}{last.shadow_ret}%</div></div>
        <div className="rounded-lg border border-line p-3"><div className="text-sm text-muted">差距（實際 − 影子）</div>
          <div className={`num text-xl font-bold ${tone(s.diff)}`}>{fmtInt(s.diff)} 元</div></div>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <div>
          <div className="mb-1 text-sm font-medium">差距從哪裡來</div>
          <table className="w-full text-sm"><tbody>
            {s.breakdown.map((b: Json) => (
              <tr key={b.name} className="border-b border-line/60"><td className="py-1">{b.name}</td>
                <td className={`num text-right ${tone(b.amount)}`}>{fmtInt(b.amount)}</td></tr>
            ))}
          </tbody></table>
          <div className="mt-2 text-xs text-muted">影子目前持股：{s.shadow_positions.map((p: Json) => `${p.code} ${p.name}`).join('、')}</div>
          <p className="mt-1 text-xs text-muted">{s.note}</p>
        </div>
        <div>
          {ex?.orders?.length > 0 && (<>
            <div className="mb-1 text-sm font-medium">{ex.today?.slice(5).replace('-', '/')} 開盤預掛（{ex.source}）</div>
            <ul className="mb-3 space-y-1 text-sm">{ex.orders.map((o: Json) => <li key={o.code + o.action}>{o.text}</li>)}</ul>
          </>)}
          <div className="mb-1 text-sm font-medium">成交價比開盤價差最多的幾筆</div>
          <table className="w-full text-sm"><tbody>
            {s.worst_fills.slice(0, 6).map((f: Json, i: number) => (
              <tr key={i} className="border-b border-line/60">
                <td className="py-1 text-muted">{f.date.slice(5)}</td><td>{f.side === 'BUY' ? '買' : '賣'} {f.code} {f.name}</td>
                <td className="num text-right">{f.price}／開 {f.open}</td>
                <td className={`num text-right ${tone(-f.cost_vs_open)}`}>{fmtInt(-f.cost_vs_open)}</td>
              </tr>
            ))}
          </tbody></table>
        </div>
      </div>
    </Section>
  )
}
