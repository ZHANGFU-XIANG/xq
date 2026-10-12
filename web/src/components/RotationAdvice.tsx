import { useQuery } from '@tanstack/react-query'
import { Section, StockLink } from './ui'
import { get, type Json } from '../lib/api'
import { fmtPct } from '../lib/format'
import { liveEvery } from '../lib/live'

const RULE: Record<string, string> = { 獲利結單: '獲利 ≥10%，今天就可以結單', 換股: '持有滿 10 個交易日、優勢 ≥1%', 到期換股: '持有滿 40 個交易日' }

/** 結單轉股：全部賣出 A、同日買進綜合排名更好的 B（與「庫存怎麼處理」同一份資料，結論一致）。 */
export default function RotationAdvice() {
  const { data: d } = useQuery<Json>({ queryKey: ['holding-advice'], queryFn: () => get('/api/holding-advice'), refetchInterval: liveEvery(60_000, 300_000) })
  if (!d) return null
  const swaps: Json[] = d.items.filter((i: Json) => i.swap)
  return (
    <Section title="結單轉股建議" aside="全部賣出、同日換買綜合排名更好的股票">
      {!swaps.length ? (
        <p className="text-[0.95rem] text-ink-2">
          目前沒有值得換的持股：持有未滿 10 個交易日（獲利未達 10%），或綜合排名最好的股票扣掉換股成本 0.585% 後優勢不夠。
        </p>
      ) : (
        <ul className="space-y-3">
          {swaps.map((i) => {
            const s = i.swap
            return (
              <li key={i.code} className="rounded-lg border border-line px-4 py-3">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-sm text-muted">全部賣出</span><StockLink code={i.code} name={i.name} />
                  <span className={`num text-sm ${i.pnl_pct >= 0 ? 'text-up' : 'text-down'}`}>（{fmtPct(i.pnl_pct, 1)}）</span>
                  <span className="text-muted">→</span>
                  <span className="text-sm text-muted">買進</span><StockLink code={s.to.code} name={s.to.name} />
                  <span className="num ml-auto font-bold text-up">多 {fmtPct(s.gain_pct, 1)}</span>
                </div>
                <div className="mt-1 text-sm text-ink-2">
                  {RULE[s.rule] ?? s.rule}・{s.to.name} 綜合預期 {fmtPct(s.to.score, 1)}、勝率 {Math.round((s.to.p_up ?? 0) * 100)}%、可信度{s.to.confidence}
                </div>
                <div className="mt-1 text-sm">{s.orders.map((o: Json) => o.text + `（限價 ${o.limit_price}）`).join(' ／ ')}</div>
                <div className={`mt-1 text-xs ${s.compliance_ok ? 'text-down' : 'text-up'}`}>
                  {s.compliance_ok ? '競賽規則檢查通過（持股率、持有檔數）' : `競賽規則：${s.compliance_notes.join('；')}`}・{s.tip}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </Section>
  )
}
