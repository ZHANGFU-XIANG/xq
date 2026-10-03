import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, type Json } from '../lib/api'
import { qty } from '../lib/format'
import { useIntl, useQuotes } from '../lib/live'
import { IntlNote, LivePrice } from './LiveBits'
import { BasketTag } from './ui'

const STATUS_STYLE: Record<string, string> = {
  可以下單: 'bg-up text-white', 可預掛單: 'bg-link text-white', 已成交: 'bg-down text-white', 接近限價: 'bg-mist text-ink-2 border border-line',
  不追價: 'bg-mist text-ink-2 border border-line', 漲停買不到: 'bg-warn/20 text-warn', 跌停賣不掉: 'bg-warn/20 text-warn',
  無報價: 'bg-mist text-muted',
}
const CHANGE_STYLE: Record<string, string> = { 新增: 'text-up', 取消: 'text-down', 數量調整: 'text-warn' }

/** 盤中建議：每 40 分鐘一個時段重新計算（沒有持股時就是建倉建議）；價格與國際影響即時更新。 */
export default function Intraday() {
  const qc = useQueryClient()
  const { data: d, isFetching } = useQuery<Json>({ queryKey: ['intraday'], queryFn: () => get('/api/intraday'), refetchInterval: 60_000 })
  const codes: string[] = (d?.orders ?? []).map((o: Json) => o.code)
  const { data: q } = useQuotes(codes)
  const { data: intl } = useIntl(codes)
  if (!d) return <p className="mb-6 text-muted">計算盤中建議中（首次約 30 秒）</p>
  const quotes: Json = q?.quotes ?? {}
  const notes: Json = intl?.impacts ?? {}
  const slot = d.slot?.slice(-5)
  return (
    <section className="mb-8 rounded-lg border border-line bg-paper p-4" aria-label="盤中建議">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-bold">
          {d.building ? '建倉建議' : '盤中建議'}
          <span className="ml-2 text-sm font-normal text-muted">{['盤前', '休市', '收盤後'].includes(slot) ? (slot === '盤前' ? '開盤前' : slot) : `${slot} 時段`}・每 40 分鐘更新{d.is_last_check ? '・收盤前檢查' : ''}</span>
        </h2>
        <span className="flex items-center gap-3 text-sm text-muted">
          <span>實際持股率 <b className={d.holding_ratio < 0.7 ? 'text-warn' : 'text-ink'}>{(d.holding_ratio * 100).toFixed(1)}%</b></span>
          <span>攻守 {Math.round(d.allocation.offense * 100)}／{Math.round(d.allocation.defense * 100)}</span>
          <button disabled={isFetching} onClick={() => get('/api/intraday?refresh=true').then((v) => qc.setQueryData(['intraday'], v))}
            className="rounded-md border border-line px-2 py-0.5 text-ink disabled:opacity-50">立即重算</button>
        </span>
      </div>
      <p className="title mb-3 text-lg">{d.summary}</p>
      {d.actions.filter((a: string) => !/^【(建倉|買進|賣出|預掛)】/.test(a)).length > 0 && (
        <ul className="mb-4 space-y-1.5">
          {d.actions.filter((a: string) => !/^【(建倉|買進|賣出|預掛)】/.test(a)).map((a: string, i: number) => <li key={i} className="rounded-md bg-up/5 px-3 py-1.5 font-medium">{a}</li>)}
        </ul>
      )}
      {d.orders.length > 0 && (
        <ul className="divide-y divide-line/60">
          {d.orders.map((o: Json) => (
            <li key={o.code + o.action} className="py-2.5 text-[0.95rem]">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_STYLE[o.status] ?? 'bg-mist'}`}>{o.status}</span>
                {o.action === 'BUY' ? <BasketTag basket={o.basket} /> : <span className="rounded-full bg-down px-2 py-0.5 text-xs text-white">賣出</span>}
                <span className="font-medium"><span className="num text-muted">{o.code}</span> {o.name}</span>
                <span className="num text-sm">{qty(Math.floor(o.planned_shares / 1000), o.planned_shares % 1000)}・限價 {o.limit_price}</span>
                <LivePrice q={quotes[o.code]} />
                <IntlNote notes={notes[o.code]} />
              </div>
              <div className="mt-0.5 text-sm text-ink-2">{o.advice}</div>
            </li>
          ))}
        </ul>
      )}
      {d.changes?.length > 0 && (
        <div className="mt-3 text-sm">
          <h3 className="mb-1 font-medium text-muted">和早上計畫相比</h3>
          <ul className="space-y-0.5">
            {d.changes.map((c: Json, i: number) => (
              <li key={i}><b className={CHANGE_STYLE[c.change] ?? ''}>{c.change}</b>　{c.code} {c.name}：{c.detail}</li>
            ))}
          </ul>
        </div>
      )}
      {d.skipped?.length > 0 && (
        <p className="mt-2 text-sm text-ink-2">今天不追：{d.skipped.map((s: Json) => `${s.code} ${s.name}（${s.reason}）`).join('、')}</p>
      )}
      {d.rule_notes?.filter((a: Json) => a.level !== '嚴重').map((a: Json, i: number) => (
        <p key={i} className="mt-2 text-sm text-warn">競賽規則：{a.title}；{a.action}</p>
      ))}
      {d.ratio_note && !d.actions.some((a: string) => a.startsWith('【持股率】')) && <p className="mt-2 text-sm text-ink-2">{d.ratio_note}</p>}
    </section>
  )
}
