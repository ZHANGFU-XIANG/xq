import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtPct, tone } from '../lib/format'
import { isTradingNow } from '../lib/live'
import { Section, StockLink } from './ui'

const LEVEL: Record<string, string> = {
  嚴重: 'border-up bg-up/5',
  注意: 'border-warn bg-warn/5',
  利多: 'border-down bg-down/5',
}

/** 隔夜／夜盤：收盤後到隔天開盤前每分鐘更新（台指期夜盤、美股、對持股開盤的估計影響、新消息），新提醒發瀏覽器通知。 */
export default function OvernightWatch() {
  const { data: d } = useQuery<Json>({
    queryKey: ['overnight'], queryFn: () => get('/api/overnight'),
    refetchInterval: () => (isTradingNow() ? false : 60_000), enabled: !isTradingNow(),
  })
  const [perm, setPerm] = useState('Notification' in window ? Notification.permission : 'denied')
  const seen = useRef<Set<string> | null>(null)

  useEffect(() => {
    if (!d?.alerts) return
    const keys = new Set<string>(d.alerts.map((a: Json) => a.key))
    if (seen.current && perm === 'granted') {
      for (const a of d.alerts as Json[]) if (!seen.current.has(a.key)) new Notification(a.title, { body: a.body, tag: `night-${a.key}` })
    }
    seen.current = keys
  }, [d, perm])

  if (!d?.active) return null
  const tx = d.tx_night
  const imp = d.impact ?? {}
  return (
    <Section title={`隔夜／夜盤（${String(d.target_date).slice(5).replace('-', '/')} 開盤前）`}
      aside={<span className="text-sm text-muted">每分鐘更新・{d.time}</span>}>
      <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-lg border border-line p-3">
          <div className="text-sm text-muted">台指期夜盤 {d.night_open ? '' : '（已收）'}</div>
          <div className={`num text-xl font-bold ${tone(tx?.chg_pct)}`}>{tx ? fmtInt(tx.price) : '—'}</div>
          <div className={`num text-sm ${tone(tx?.chg_pct)}`}>{tx ? `${tx.change > 0 ? '+' : ''}${fmtInt(tx.change)} 點（${fmtPct(tx.chg_pct)}）` : '尚無報價'}</div>
          {tx && <div className="text-xs text-muted">相對日盤結算 {fmtInt(tx.ref)}・{String(tx.time).slice(11, 16)}</div>}
        </div>
        <div className="rounded-lg border border-line p-3">
          <div className="text-sm text-muted">估計開盤影響（持股＋期貨）</div>
          <div className={`num text-xl font-bold ${tone(imp.total)}`}>{imp.total > 0 ? '+' : ''}{fmtInt(imp.total)}</div>
          <div className="text-xs text-muted">股票 {fmtInt(imp.stock_amount)}・期貨 {fmtInt(imp.futures_amount)}</div>
          <div className="text-xs text-muted">{imp.basis}</div>
        </div>
        {(d.us ?? []).filter((u: Json) => u.ticker === '^SOX' || u.ticker === 'NQ=F').map((u: Json) => (
          <div key={u.ticker} className="rounded-lg border border-line p-3">
            <div className="text-sm text-muted">{u.name}</div>
            <div className={`num text-xl font-bold ${tone(u.chg_pct)}`}>{fmtPct(u.chg_pct)}</div>
            <div className="text-xs text-muted">{u.price}・{u.time}</div>
          </div>
        ))}
      </div>

      {'Notification' in window && perm === 'default' && (
        <button className="mb-2 rounded-md border border-line px-2 py-0.5 text-xs" onClick={() => Notification.requestPermission().then(setPerm)}>開啟通知</button>
      )}
      {d.alerts?.length > 0 ? (
        <ul className="mb-3 space-y-2">
          {d.alerts.map((a: Json) => (
            <li key={a.key} className={`rounded-lg border-l-4 px-4 py-2 ${LEVEL[a.level] ?? LEVEL['注意']}`}>
              <div className="font-bold">［{a.level}］{a.title}</div>
              <div className="text-sm text-ink-2">{a.body}</div>
            </li>
          ))}
        </ul>
      ) : <p className="mb-3 text-sm text-muted">目前沒有需要注意的隔夜狀況（夜盤跌 1%、費半跌 3%、美股期貨跌 1.5% 等才會提醒，Email 也會同步寄出）。</p>}

      <div className="grid items-start gap-4 md:grid-cols-2">
        <div>
          <div className="mb-1 text-sm text-muted">美股與匯率（含盤前盤後）</div>
          <table className="w-full text-sm">
            <tbody>
              {(d.us ?? []).map((u: Json) => (
                <tr key={u.ticker} className="border-b border-line/60">
                  <td className="py-1">{u.name}</td>
                  <td className="num text-right">{u.price}</td>
                  <td className={`num text-right ${tone(u.chg_pct)}`}>{fmtPct(u.chg_pct)}</td>
                  <td className="text-right text-xs text-muted">{u.time}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mb-1 mt-3 text-sm text-muted">持股估計開盤跳空（β × 夜盤）</div>
          <table className="w-full text-sm">
            <tbody>
              {(imp.items ?? []).map((i: Json) => (
                <tr key={i.code} className="border-b border-line/60">
                  <td className="py-1"><StockLink code={i.code} name={i.name} /></td>
                  <td className="num text-right text-muted">β {i.beta}</td>
                  <td className={`num text-right ${tone(i.est_gap_pct)}`}>{fmtPct(i.est_gap_pct)}</td>
                  <td className={`num text-right ${tone(i.amount)}`}>{fmtInt(i.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          {d.holding_news?.length > 0 && (<>
            <div className="mb-1 text-sm text-muted">持股相關負面消息</div>
            <ul className="mb-3 space-y-1 text-sm">
              {d.holding_news.map((n: Json) => (
                <li key={n.code + n.title}><span className="font-medium">{n.code} {n.name}</span>
                  <a className="underline decoration-line" href={n.url} target="_blank" rel="noreferrer">{n.issue}</a>
                  <span className="ml-1 text-xs text-muted">{n.time}</span></li>
              ))}
            </ul>
          </>)}
          <div className="mb-1 text-sm text-muted">收盤後新聞（開盤消息整理，每 5 分鐘更新）</div>
          {d.news?.length ? (
            <ul className="space-y-1 text-sm">
              {d.news.map((n: Json) => (
                <li key={n.url}><span className="num mr-1 text-xs text-muted">{n.time}</span>
                  <a className="hover:underline" href={n.url} target="_blank" rel="noreferrer">{n.title}</a>
                  <span className="ml-1 text-xs text-muted">{n.source}</span></li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted">收盤後還沒有新的新聞。</p>}
        </div>
      </div>
    </Section>
  )
}
