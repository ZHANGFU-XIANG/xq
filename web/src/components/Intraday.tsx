import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Json } from '../lib/api'
import { qty } from '../lib/format'
import { isTradingNow, useIntl, useQuotes } from '../lib/live'
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
  // 拿到的是上一份（stale）時每 10 秒問一次，背景算好就換上；盤中每 30 秒（伺服器每 10 分鐘重算），盤後每 2 分鐘
  return useQuery<Json>({ queryKey: ['intraday'], queryFn: () => get('/api/intraday'),
    refetchInterval: (q) => ((q.state.data as Json)?.stale ? 10_000 : isTradingNow() ? 30_000 : 120_000) })
}

/** 盤中建議：網站盤中每 10 分鐘以即時價重新計算（雲端排程與 Email 每 40 分鐘）（沒有持股時就是建倉建議）。asset 指定只顯示股票、ETF 或期貨與選擇權。 */
export default function Intraday({ asset }: { asset: Asset }) {
  const qc = useQueryClient()
  const { data: d, isFetching } = useIntraday()
  const rows: Json[] = (d?.orders ?? []).filter((o: Json) => assetOf(o) === asset)
  const codes = [...rows.map((o) => o.code), ...(d?.recommendations ?? []).map((r: Json) => r.code)]
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
  const recs: Json[] = (d.recommendations ?? []).filter((r: Json) => assetOf(r) === asset)
  const holdCount = (d.holding_items ?? []).filter((h: Json) => assetOf(h) === asset && h.verdict === '續抱').length
  return (
    <section className="mb-8 rounded-lg border border-line bg-paper p-4" aria-label={`${ASSET_LABEL[asset]}盤中建議`}>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-bold">
          {ASSET_LABEL[asset]}・{d.building ? '建倉建議' : '持股建議'}
          <span className="ml-2 text-sm font-normal text-muted">{['盤前', '休市', '收盤後'].includes(slot) ? (slot === '盤前' ? '開盤前' : slot) : `${slot} 時段`}・盤中每 10 分鐘重算{d.is_last_check ? '・收盤前檢查' : ''}</span>
        </h2>
        <span className="flex items-center gap-3 text-sm text-muted">
          <span>實際持股率 <b className={d.holding_ratio < 0.7 ? 'text-warn' : 'text-ink'}>{(d.holding_ratio * 100).toFixed(1)}%</b></span>
          <button disabled={isFetching} onClick={() => get('/api/intraday?refresh=true').then((v) => qc.setQueryData(['intraday'], v))}
            className="rounded-md border border-line px-2 py-0.5 text-ink disabled:opacity-50">立即重算</button>
        </span>
      </div>
      {d.stale && (
        <p className="mb-3 rounded-md bg-mist px-3 py-1.5 text-sm text-ink-2">先顯示 {d.cached_from} 計算的結果，正在依最新報價重算（約 1 分鐘），完成後自動更新</p>
      )}
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
            {rows.length ? `${ASSET_LABEL[asset]} ${rows.length} 筆建議，${ready} 筆現在可以下單`
              : d.building ? `${ASSET_LABEL[asset]}目前沒有建議`
              : holdCount ? `${ASSET_LABEL[asset]} ${holdCount} 檔持股都建議續抱，不需賣出` : `${ASSET_LABEL[asset]}目前沒有持股需要處理`}
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
          {!d.building && recs.length > 0 && (
            <div className="mt-4">
              <h3 className="mb-1 font-medium">近期推薦<span className="ml-2 text-sm font-normal text-muted">已有庫存，以下僅供參考，不是下單指令</span></h3>
              {d.cash_now && (
                <p className="mb-1 text-sm text-ink-2">
                  可用資金約 {Math.round(d.cash_now.available).toLocaleString()} 元（證券戶 {Math.round(d.cash_now.securities).toLocaleString()}、
                  期貨戶可轉出 {Math.round(d.cash_now.futures_transferable).toLocaleString()}、建議賣出可回收 {Math.round(d.cash_now.sell_proceeds).toLocaleString()}）；
                  不夠時下方會列出要先賣哪檔、幾張
                </p>
              )}
              <ul className="divide-y divide-line/60">
                {recs.map((r) => (
                  <li key={r.code} className="py-2 text-[0.95rem]">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <BasketTag basket={r.basket} />
                      <Link to={`/stock/${r.code}`} className="font-medium text-ink no-underline hover:text-link"><span className="num text-muted">{r.code}</span> {r.name}</Link>
                      <span className="num text-sm">參考 {qty(Math.floor(r.shares / 1000), r.shares % 1000)}・限價 {r.limit_price}{r.stop ? `・停損 ${r.stop}` : ''}</span>
                      <LivePrice q={quotes[r.code]} />
                    </div>
                    <div className="mt-0.5 text-sm text-ink-2">{r.why.join('；')}｜{r.entry_note}</div>
                    {r.funding && (
                      <div className="mt-0.5 text-sm">
                        <span className="text-muted">資金：需要約 {Math.round(r.funding.need).toLocaleString()} 元，現金可支應 {Math.round(r.funding.from_cash).toLocaleString()} 元</span>
                        {r.funding.sells.map((x: Json) => (
                          <div key={x.code} className={x.worth ? 'text-ink' : 'text-muted'}>先賣 {x.code} {x.name}：{x.note}</div>
                        ))}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
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
