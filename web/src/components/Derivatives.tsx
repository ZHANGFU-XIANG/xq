import type { Json } from '../lib/api'
import { fmtInt } from '../lib/format'

/** 期貨與選擇權建議：方向模型、回測門檻、建議口數、賣權避險。 */
export default function Derivatives({ d, compact = false }: { d?: Json; compact?: boolean }) {
  if (!d) return null
  const sig = d.signal
  const q = d.quote
  const bt = sig?.backtest
  return (
    <div className="rounded-lg border border-fut/40 bg-fut/5 px-4 py-3">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-bold text-fut">期貨與選擇權：{d.summary}</h3>
        <span className="text-sm text-muted">
          {d.mode && `模式：${d.mode}`}
          {q?.price ? `・${q.contract} ${fmtInt(q.price)}${q.basis != null ? `（基差 ${q.basis > 0 ? '+' : ''}${q.basis}）` : ''}` : ''}
          {d.reserve ? `・期貨戶留存 ${fmtInt(d.reserve)} 元` : ''}
        </span>
      </div>
      {d.orders?.map((o: Json) => (
        <div key={o.contract} className="mt-2 rounded-md bg-paper px-3 py-2 text-[0.95rem]">
          <div className="flex flex-wrap items-baseline gap-x-3">
            <span className={`rounded px-1.5 py-0.5 text-xs font-medium text-white ${o.action === 'BUY' ? 'bg-up' : 'bg-down'}`}>{o.action === 'BUY' ? '做多' : '放空'}</span>
            <span className="font-medium">{o.name} {o.contract}</span>
            <span className="num">{o.qty} 口・限價 {fmtInt(o.limit_price)}・停損 {fmtInt(o.stop)}・停利 {fmtInt(o.take_profit)}</span>
            <span className="num text-sm text-muted">保證金 {fmtInt(o.margin)}・最大損失 {fmtInt(o.max_loss)} 元</span>
          </div>
          {!compact && (
            <ul className="mt-1 list-disc pl-5 text-sm text-ink-2">
              {[...o.why, ...o.why_qty, ...o.exit_plan].map((w: string, i: number) => <li key={i}>{w}</li>)}
            </ul>
          )}
        </div>
      ))}
      {d.hedge && (
        <div className="mt-2 rounded-md bg-paper px-3 py-2 text-[0.95rem]">
          <div className="flex flex-wrap items-baseline gap-x-3">
            <span className="rounded bg-defense px-1.5 py-0.5 text-xs font-medium text-white">避險</span>
            <span className="font-medium">買進 {d.hedge.name}（{d.hedge.contract}）</span>
            <span className="num">{d.hedge.qty} 口・權利金 {d.hedge.limit_price} 點・合計 {fmtInt(d.hedge.premium)} 元（最大損失）</span>
          </div>
          {!compact && <ul className="mt-1 list-disc pl-5 text-sm text-ink-2">{d.hedge.why.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul>}
        </div>
      )}
      {d.conditions?.length > 0 && (
        <div className="mt-3">
          <h4 className="mb-1 text-sm font-medium text-muted">什麼情況下會建議操作（每 40 分鐘重算）</h4>
          <ul className="space-y-1.5 text-[0.95rem]">
            {d.conditions.map((c: Json) => (
              <li key={c.name} className="rounded-md bg-paper px-3 py-2">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${c.met ? 'bg-up text-white' : 'bg-mist text-muted'}`}>{c.met ? '條件成立' : '未達'}</span>
                  <span className="font-medium">{c.name}</span>
                </div>
                <div className="mt-0.5 text-sm text-ink-2">條件：{c.need}</div>
                <div className="text-sm text-ink-2">目前：{c.now}</div>
              </li>
            ))}
          </ul>
        </div>
      )}
      {!compact && d.reference && !d.orders?.length && (
        <div className="mt-3 rounded-md border border-dashed border-line px-3 py-2 text-sm">
          <div className="font-medium text-ink-2">{d.reference.note}</div>
          <ul className="mt-1 space-y-0.5 text-ink-2">
            {d.reference.futures.map((r: Json) => (
              <li key={r.contract}>{r.name} {r.contract} 1 口：約 {fmtInt(r.price)} 點、契約價值 {fmtInt(r.notional)} 元、原始保證金 {fmtInt(r.margin)} 元；
                停損 {fmtInt(r.stop_points)} 點時虧損約 {fmtInt(r.loss_at_stop)} 元（總資產 {r.loss_pct_nav}%）</li>
            ))}
            {d.reference.put && <li>台指賣權 {d.reference.put.contract}：權利金 {d.reference.put.price} 點 × 50 ＝ {fmtInt(d.reference.put.premium)} 元／口，加權跌破 {fmtInt(d.reference.put.protect_from)} 點開始有保護，最多虧掉權利金</li>}
          </ul>
          <div className="mt-1 text-xs text-muted">{d.reference.stop_rule}</div>
        </div>
      )}
      {!d.orders?.length && !d.hedge && !d.conditions?.length && (
        <ul className="mt-1 space-y-0.5 text-sm text-ink-2">{(d.reasons ?? []).map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>
      )}
      {!compact && bt && (
        <p className="mt-2 text-xs text-muted">
          方向模型回測（{bt.period}，樣本外每 {bt.h} 日）：一直做多勝率 {Math.round((bt.always_long?.hit ?? 0) * 100)}%、平均 {bt.always_long?.avg_ret_pct}%；
          模型做多 {bt.long?.n} 次、勝率 {bt.long?.hit != null ? Math.round(bt.long.hit * 100) : '—'}%、平均 {bt.long?.avg_ret_pct ?? '—'}%。
          模型要贏過「一直做多」才會開放擇時建議。
        </p>
      )}
    </div>
  )
}
