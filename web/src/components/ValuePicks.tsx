import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtRatio } from '../lib/format'
import { Section, StockLink } from './ui'

const CAPS = [30, 50, 100]

/** 平價潛力股：股價低、模型看漲且波動與回檔小的股票排名。 */
export default function ValuePicks() {
  const [cap, setCap] = useState(50)
  const { data, isLoading } = useQuery<Json>({ queryKey: ['value-picks', cap], queryFn: () => get(`/api/value-picks?max_price=${cap}`), staleTime: 600_000 })
  return (
    <Section title="平價潛力股" aside={
      <span className="inline-flex gap-1 text-sm" role="group" aria-label="股價上限">
        {CAPS.map((c) => (
          <button key={c} onClick={() => setCap(c)} aria-pressed={cap === c}
            className={`rounded-md px-2 py-0.5 ${cap === c ? 'bg-link text-white' : 'border border-line text-ink-2'}`}>≤{c} 元</button>
        ))}
      </span>
    }>
      {isLoading || !data ? <p className="text-sm text-muted">計算中…</p> : (
        <>
          <p className="mb-2 text-sm text-ink-2">
            條件：一個月收正機率 ≥ 全市場平均 {fmtRatio(data.base_rate)}、預期回檔 ≥ −10%、日波動 ≤ 3.5%、β ≤ 1.5、不過熱、非注意／處置股。
            共 {data.n_pass} 檔通過（{data.asof} 收盤資料）。
          </p>
          {!data.items.length ? <p className="text-sm text-muted">這個價位目前沒有符合條件的股票。</p> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[680px] text-[0.95rem]">
                <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
                  <th className="py-2 font-normal">#</th><th className="font-normal">標的</th><th className="text-right font-normal">股價</th>
                  <th className="text-right font-normal">一張</th><th className="text-right font-normal">收正機率</th>
                  <th className="text-right font-normal">預期報酬</th><th className="text-right font-normal">預期回檔</th>
                  <th className="text-right font-normal">日波動</th>
                </tr></thead>
                <tbody>
                  {data.items.map((x: Json) => (
                    <tr key={x.code} className="border-b border-line/70">
                      <td className="num py-2 text-muted">{x.rank}</td>
                      <td><StockLink code={x.code} name={x.name} />
                        <div className="text-xs text-muted">{x.sector}</div></td>
                      <td className="num text-right">{x.price}</td>
                      <td className="num text-right">{fmtInt(x.lot_cost)}</td>
                      <td className="num text-right">{fmtRatio(x.p_up)}</td>
                      <td className="num text-right text-up">{fmtRatio(x.exp_ret)}</td>
                      <td className="num text-right text-down">{fmtRatio(x.exp_dd)}</td>
                      <td className="num text-right">{x.vol20_pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-2 text-xs text-muted">{data.note}</p>
        </>
      )}
    </Section>
  )
}
