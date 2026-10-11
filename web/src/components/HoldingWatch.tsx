import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { get, type Json } from '../lib/api'
import { fmtPct, tone } from '../lib/format'
import { Section, StockLink } from './ui'
import { liveEvery } from '../lib/live'

const STYLE: Record<string, string> = {
  盡快賣出: 'border-up bg-up/5 text-up',
  減碼: 'border-warn bg-warn/5 text-warn',
  注意: 'border-warn bg-warn/5 text-warn',
  低檔可望反彈: 'border-down bg-down/5 text-down',
}

function canNotify() {
  return typeof window !== 'undefined' && 'Notification' in window
}

/** 持股動向：盤中每分鐘檢查，有持股新進入「盡快賣出」或「低檔可望反彈」時發出瀏覽器通知。 */
export default function HoldingWatch() {
  const { data } = useQuery<Json>({ queryKey: ['holding-watch'], queryFn: () => get('/api/holding-watch'), refetchInterval: liveEvery(20_000, 120_000) })
  const [perm, setPerm] = useState(canNotify() ? Notification.permission : 'denied')
  const prev = useRef<Record<string, string> | null>(null)

  useEffect(() => {
    if (!data?.items) return
    const now: Record<string, string> = Object.fromEntries(data.items.map((i: Json) => [i.code, i.status]))
    if (prev.current && data.trading && perm === 'granted') {
      for (const i of data.items as Json[]) {
        const was = prev.current[i.code]
        if (was !== i.status && (i.status === '盡快賣出' || i.status === '低檔可望反彈')) {
          new Notification(`${i.status}：${i.code} ${i.name}`, { body: i.reasons.join('；'), tag: `watch-${i.code}` })
        }
      }
    }
    prev.current = now
  }, [data, perm])

  if (!data?.items?.length) return null
  const flagged: Json[] = data.items.filter((i: Json) => i.status !== '正常')
  const c = data.counts ?? {}
  return (
    <Section title="持股動向" aside={<span className="text-sm text-muted">{data.trading ? '盤中每 20 秒更新' : '非交易時段'}・{data.time}</span>}>
      <p className="mb-2 text-sm text-ink-2">
        <span className="text-up">盡快賣出 {c['盡快賣出'] ?? 0}</span>　{(c['減碼'] ?? 0) > 0 && <><span className="text-warn">減碼 {c['減碼']}</span>　</>}<span className="text-warn">注意 {c['注意'] ?? 0}</span>
        <span className="text-down">低檔可望反彈 {c['低檔可望反彈'] ?? 0}</span>　<span className="text-muted">正常 {c['正常'] ?? 0}</span>
        {canNotify() && perm !== 'granted' && perm !== 'denied' && (
          <button className="ml-3 rounded-md border border-line px-2 py-0.5 text-xs" onClick={() => Notification.requestPermission().then(setPerm)}>
            開啟通知
          </button>
        )}
      </p>
      {flagged.length === 0 ? <p className="text-sm text-muted">目前沒有需要處理的持股。</p> : (
        <ul className="space-y-2">
          {flagged.map((i) => (
            <li key={i.code} className={`rounded-lg border-l-4 px-4 py-2 ${STYLE[i.status]}`}>
              <div className="flex flex-wrap items-baseline gap-x-3">
                <span className="font-bold">{i.status}</span>
                <StockLink code={i.code} name={i.name} />
                <span className="num text-ink">{i.price}</span>
                {i.chg_pct != null && <span className={`num ${tone(i.chg_pct)}`}>{fmtPct(i.chg_pct)}</span>}
                <span className={`num text-sm ${tone(i.pnl_pct)}`}>持有 {fmtPct(i.pnl_pct)}</span>
              </div>
              <div className="mt-0.5 text-sm text-ink-2">{i.reasons.join('；')}</div>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-xs text-muted">{data.note}</p>
    </Section>
  )
}
