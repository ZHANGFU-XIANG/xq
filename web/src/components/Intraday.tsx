import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Json } from '../lib/api'
import { qty } from '../lib/format'
import { useIntl, useQuotes } from '../lib/live'
import { assetOf, ASSET_LABEL, type Asset } from './AssetTabs'
import Derivatives from './Derivatives'
import { IntlNote, LivePrice } from './LiveBits'
import { BasketTag } from './ui'

const STATUS_STYLE: Record<string, string> = {
  可以下單: 'bg-up text-white', 可預掛單: 'bg-link text-white', 已成交: 'bg-down text-white', 接近限價: 'bg-mist text-ink-2 border border-line',
  不追價: 'bg-mist text-ink-2 border border-line', 漲停買不到: 'bg-warn/20 text-warn', 跌停賣不掉: 'bg-warn/20 text-warn',
  無報價: 'bg-mist text-muted',
}
const CHANGE_STYLE: Record<string, string> = { 新增: 'text-up', 取消: 'text-down', 數量調整: 'text-warn' }
const ORDER_ACTION = /^【(建倉|買進|賣出|預掛)】/

export function useIntraday() {
  return useQuery<Json>({ queryKey: ['intraday'], queryFn: () => get('/api/intraday'), refetchInterval: 60_000 })
}

/** 盤中建議：每 40 分鐘一個時段重新計算（沒有持股時就是建倉建議）。asset 指定只顯示股票、ETF 或期貨與選擇權。 */
export default function Intraday({ asset }: { asset: Asset }) {
  const qc = useQueryClient()
  const { data: d, isFetching } = useIntraday()
  const rows: Json[] = (d?.orders ?? []).filter((o: Json) => assetOf(o) === asset)
  const codes = rows.map((o) => o.code)
  const { data: q } = useQuotes(codes)
  const { data: intl } = useIntl(codes)
  if (!d) return <p className="mb-6 text-muted">計算盤中建議中（首次約 30 秒）</p>
  const quotes: Json = q?.quotes ?? {}
  const notes: Json = intl?.impacts ?? {}
  const slot: string = (d.slot ?? '').split(' ').slice(1).join(' ') || d.slot
  const general = d.actions.filter((a: string) => !ORDER_ACTION.test(a))  // 資金、持股率、競賽規則：各分頁都顯示
  const changes = (d.changes ?? []).filter((c: Json) => assetOf(c.code) === asset)
  const skipped = (d.skipped ?? []).filter((s: Json) => assetOf(s.code) === asset)
  const ready = rows.filter((o) => ['可以下單', '可預掛單'].includes(o.status)).length
  return (
    <section className="mb-8 rounded-lg border border-line bg-paper p-4" aria-label={`${ASSET_LABEL[asset]}盤中建議`}>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-bold">
          {ASSET_LABEL[asset]}・{d.building ? '建倉建議' : '盤中建議'}
          <span className="ml-2 text-sm font-normal text-muted">{['盤前', '休市', '收盤後'].includes(slot) ? (slot === '盤前' ? '開盤前' : slot) : `${slot} 時段`}・每 40 分鐘更新{d.is_last_check ? '・收盤前檢查' : ''}</span>
        </h2>
        <span className="flex items-center gap-3 text-sm text-muted">
          <span>實際持股率 <b className={d.holding_ratio < 0.7 ? 'text-warn' : 'text-ink'}>{(d.holding_ratio * 100).toFixed(1)}%</b></span>
          <button disabled={isFetching} onClick={() => get('/api/intraday?refresh=true').then((v) => qc.setQueryData(['intraday'], v))}
            className="rounded-md border border-line px-2 py-0.5 text-ink disabled:opacity-50">立即重算</button>
        </span>
      </div>
      {general.length > 0 && (
        <ul className="mb-4 space-y-1.5">
          {general.map((a: string, i: number) => <li key={i} className="rounded-md bg-up/5 px-3 py-1.5 font-medium">{a}</li>)}
        </ul>
      )}

      {asset === 'FUT' ? (
        <>
          {d.derivatives ? <Derivatives d={d.derivatives} /> : <p className="text-muted">期貨建議計算中</p>}
          <p className="mt-3 text-sm"><Link to="/futures">看期貨 K 線、各月報價與選擇權報價表</Link></p>
        </>
      ) : (
        <>
          <p className="title mb-3 text-lg">
            {rows.length ? `${ASSET_LABEL[asset]} ${rows.length} 筆建議，${ready} 筆現在可以下單` : `${ASSET_LABEL[asset]}目前沒有建議`}
          </p>
          {rows.length > 0 && (
            <ul className="divide-y divide-line/60">
              {rows.map((o) => (
                <li key={o.code + o.action} className="py-2.5 text-[0.95rem]">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_STYLE[o.status] ?? 'bg-mist'}`}>{o.status}</span>
                    {o.action === 'BUY' ? <BasketTag basket={o.basket} /> : <span className="rounded-full bg-down px-2 py-0.5 text-xs text-white">賣出</span>}
                    <Link to={`/stock/${o.code}`} className="font-medium text-ink no-underline hover:text-link"><span className="num text-muted">{o.code}</span> {o.name}</Link>
                    <span className="num text-sm">{qty(Math.floor(o.planned_shares / 1000), o.planned_shares % 1000)}・限價 {o.limit_price}</span>
                    <LivePrice q={quotes[o.code]} />
                    <IntlNote notes={notes[o.code]} />
                  </div>
                  <div className="mt-0.5 text-sm text-ink-2">{o.advice}</div>
                </li>
              ))}
            </ul>
          )}
          {changes.length > 0 && (
            <div className="mt-3 text-sm">
              <h3 className="mb-1 font-medium text-muted">和早上計畫相比</h3>
              <ul className="space-y-0.5">
                {changes.map((c: Json, i: number) => (
                  <li key={i}><b className={CHANGE_STYLE[c.change] ?? ''}>{c.change}</b>　{c.code} {c.name}：{c.detail}</li>
                ))}
              </ul>
            </div>
          )}
          {skipped.length > 0 && (
            <p className="mt-2 text-sm text-ink-2">今天不追：{skipped.map((s: Json) => `${s.code} ${s.name}（${s.reason}）`).join('、')}</p>
          )}
        </>
      )}
      {d.rule_notes?.filter((a: Json) => a.level !== '嚴重').map((a: Json, i: number) => (
        <p key={i} className="mt-2 text-sm text-warn">競賽規則：{a.title}；{a.action}</p>
      ))}
      {d.ratio_note && !d.actions.some((a: string) => a.startsWith('【持股率】')) && <p className="mt-2 text-sm text-ink-2">{d.ratio_note}</p>}
    </section>
  )
}
