import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Json } from '../lib/api'
import { fmtPct, tone } from '../lib/format'
import { liveEvery } from '../lib/live'
import { useChat } from './Chat'

const STYLE: Record<string, string> = {
  可以買: 'bg-link text-white',
  等回檔再買: 'bg-warn/15 text-warn border border-warn/40',
  先不要買: 'bg-ink/10 text-ink-2 border border-line',
  續抱: 'bg-mist text-ink-2 border border-line',
  減碼: 'bg-warn text-white',
  全部出清: 'bg-ink text-paper',
  加碼: 'bg-link text-white',
}

function Card({ x }: { x: Json }) {
  const { ask } = useChat()
  return (
    <li className="rounded-lg border border-line bg-paper px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`rounded-md px-2.5 py-0.5 text-sm font-bold ${STYLE[x.verdict] ?? ''}`}>{x.verdict}</span>
        <Link to={`/stock/${x.code}`} className="font-bold text-ink no-underline hover:text-link"><span className="num text-muted">{x.code}</span> {x.name}</Link>
        {x.price != null && <span className="num text-sm text-ink-2">現價 {x.price}</span>}
        {x.pnl_pct != null && <span className={`num text-sm ${tone(x.pnl_pct)}`}>損益 {fmtPct(x.pnl_pct)}</span>}
        <button type="button" onClick={() => ask(`${x.code} ${x.name} 為什麼是「${x.verdict}」？`)} className="ml-auto text-sm">問 AI</button>
      </div>
      <p className="title mt-1.5 text-[1.02rem]">{x.headline}</p>
      {(x.reasons?.length > 0 || x.change_if?.length > 0) && (
        <ul className="mt-1 space-y-0.5 text-sm text-ink-2">
          {(x.reasons ?? []).map((r: string, i: number) => <li key={`r${i}`}>・{r}</li>)}
          {(x.change_if ?? []).map((r: string, i: number) => <li key={`c${i}`} className="text-muted">改變結論的條件：{r}</li>)}
        </ul>
      )}
    </li>
  )
}

function Group({ title, items, empty }: { title: string; items: Json[]; empty: string }) {
  return (
    <div>
      <h3 className="mb-2 text-sm font-medium text-muted">{title}（{items.length}）</h3>
      {items.length ? <ul className="space-y-2">{items.map((x) => <Card key={`${title}${x.code}`} x={x} />)}</ul>
        : <p className="text-sm text-muted">{empty}</p>}
    </div>
  )
}

/** 買賣決策總表：直接告訴使用者哪些可以買、哪些先不要買、持股為什麼續抱。 */
export default function DecisionBoard() {
  const { data: d, isLoading } = useQuery<Json>({
    queryKey: ['decisions'], queryFn: () => get('/api/decisions'), refetchInterval: liveEvery(60_000, 600_000),
  })
  if (isLoading) return <p className="py-4 text-muted">整理買賣結論中</p>
  if (!d) return null
  return (
    <section className="mb-8" aria-label="能買什麼">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2 border-b border-line pb-1.5">
        <h2 className="text-[1.05rem] font-bold">能買什麼、該抱什麼</h2>
        <span className="text-sm text-muted">{d.time}{d.stale ? '・重算中' : ''}</span>
      </div>
      <p className="mb-4 text-[0.95rem] font-medium">{d.summary}</p>
      <div className="space-y-6">
        {d.hold.length > 0 && <Group title="手上的持股" items={d.hold} empty="" />}
        <Group title="可以買" items={d.buy} empty="目前沒有價位與資金都合適、可以馬上買的標的。" />
        <Group title="等回檔再買" items={d.wait} empty="沒有。" />
        <Group title="先不要買" items={d.avoid} empty="沒有。" />
      </div>
      <p className="mt-3 text-xs text-muted">{d.note}</p>
    </section>
  )
}
