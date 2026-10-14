import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { get, post, type Json } from '../lib/api'
import { liveEvery } from '../lib/live'
import { Section } from './ui'

/** 規則倒數（收盤持股率、交易標的數、最後一日規定）＋排名追蹤（上傳每週排名截圖）。 */
export default function Standing() {
  const qc = useQueryClient()
  const { data: cc } = useQuery<Json>({ queryKey: ['close-check'], queryFn: () => get('/api/close-check'), refetchInterval: liveEvery(30_000, 600_000) })
  const { data: st } = useQuery<Json>({ queryKey: ['standing'], queryFn: () => get('/api/standing'), refetchInterval: 600_000 })
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const upload = async (f: File) => {
    setBusy(true)
    setMsg(null)
    try {
      const b64 = await new Promise<string>((ok, bad) => {
        const r = new FileReader()
        r.onload = () => ok(String(r.result))
        r.onerror = bad
        r.readAsDataURL(f)
      })
      const res = await post('/api/standing/screenshot', { image: b64 })
      const rec = res.record ?? {}
      setMsg(`讀到：第 ${rec.my_rank ?? '?'} 名、報酬 ${rec.my_return_pct ?? '?'}%、第 20 名 ${rec.rank20_return_pct ?? '?'}%（${res.message}）`)
      qc.invalidateQueries({ queryKey: ['standing'] })
    } catch (e) {
      setMsg(`讀取失敗：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="規則倒數與排名" aside={cc ? `剩 ${cc.days_left} 個交易日・${cc.time} 估算` : undefined}>
      <ul className="mb-3 space-y-1 text-sm">
        {(cc?.items ?? []).map((i: Json) => (
          <li key={i.key} className="flex items-baseline gap-2">
            <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${!i.active ? 'bg-line' : i.ok ? 'bg-down' : 'bg-up'}`} aria-hidden />
            <span className={i.active && !i.ok ? 'font-bold text-up' : ''}>{i.text}</span>
          </li>
        ))}
      </ul>
      <div className="rounded-lg border border-line p-3 text-sm">
        {st?.latest ? (
          <div className="mb-1">最新排名：第 <span className="num font-bold">{st.latest.my_rank ?? '?'}</span> 名／{st.latest.total_teams ?? '?'} 隊，
            報酬 {st.latest.my_return_pct ?? '?'}%，第 20 名 {st.latest.rank20_return_pct ?? '?'}%（{st.latest.as_of ?? st.latest.uploaded}）</div>
        ) : null}
        <ul className="mb-2 space-y-0.5 text-ink-2">{(st?.notes ?? []).map((n: string) => <li key={n}>{n}</li>)}</ul>
        <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-line px-3 py-1.5 hover:bg-mist">
          <input type="file" accept="image/*" className="hidden" disabled={busy}
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = '' }} />
          {busy ? '讀取中…' : '上傳排名截圖'}
        </label>
        {msg && <p className="mt-2 text-xs text-muted">{msg}</p>}
      </div>
      <p className="mt-2 text-xs text-muted">12:50～13:28 若持股率或標的數不足，會推播並寄信告知 13:30 前要補買多少。每週五排名公布後上傳截圖，系統會算出距前 20 名與正報酬的差距。</p>
    </Section>
  )
}
