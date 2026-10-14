import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { get, type Json } from '../lib/api'
import { fmtInt } from '../lib/format'
import { liveEvery } from '../lib/live'

const STYLE: Record<string, { box: string; dot: string }> = {
  綠燈: { box: 'border-down/50 bg-down/5', dot: 'bg-down' },
  灰燈: { box: 'border-line bg-paper', dot: 'bg-muted' },
  黃燈: { box: 'border-warn/60 bg-warn/10', dot: 'bg-warn' },
  紅燈: { box: 'border-up/60 bg-up/10', dot: 'bg-up' },
}

/** 大盤攻守燈號與撤資預警（/api/regime）。綠＝積極進攻、灰＝中性、黃＝預警先暫停追高、紅＝轉弱提早撤資。 */
export default function RegimeBanner({ compact = false }: { compact?: boolean }) {
  const { data: r } = useQuery<Json>({ queryKey: ['regime'], queryFn: () => get('/api/regime'), refetchInterval: liveEvery(10_000, 300_000) })
  const [open, setOpen] = useState(false)
  if (!r) return null
  const st = STYLE[r.level] ?? STYLE['灰燈']
  const L = r.lines
  return (
    <section className={`mb-6 rounded-lg border-2 px-4 py-3 ${st.box}`} aria-label="大盤攻守燈號">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`inline-block h-3 w-3 shrink-0 rounded-full ${st.dot}`} aria-hidden />
        <strong className="text-[1.02rem]">{r.headline}</strong>
        <span className="text-sm text-muted">{r.live_taiex ? `加權 ${fmtInt(r.live_taiex)}・` : ''}{r.time}</span>
      </div>
      {r.warnings?.length > 0 && (
        <ul className="mt-2 space-y-0.5 pl-6 text-sm">
          {r.warnings.map((w: string) => <li key={w} className="list-disc">{w}</li>)}
        </ul>
      )}
      {!compact && (
        <ul className="mt-2 space-y-0.5 pl-6 text-sm text-ink-2">
          {r.actions.map((a: string) => <li key={a} className="list-disc">{a}</li>)}
        </ul>
      )}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span>轉紅燈（撤資）：收盤跌破 <b className="num text-up">{fmtInt(L.weak_enter)}</b></span>
        <span>紅燈解除：站回 <b className="num">{fmtInt(L.weak_exit)}</b></span>
        <span>綠燈（加攻）：站上 <b className="num text-down">{fmtInt(L.strong_enter)}</b>（且 20 日漲 &gt; 2%）</span>
        <span>綠燈解除：跌破 <b className="num">{fmtInt(L.strong_exit)}</b></span>
        <button className="text-link" onClick={() => setOpen((o) => !o)}>{open ? '收起依據' : '回測依據'}</button>
      </div>
      {open && (
        <p className="mt-2 text-xs leading-relaxed text-ink-2">
          {r.backtest.period}：{r.backtest.adopted.name} 年化 {r.backtest.adopted.cagr}%、最大回檔 {r.backtest.adopted.max_dd}%、
          38 日平均 {r.backtest.adopted.avg38}%；{r.backtest.baseline.name} 年化 {r.backtest.baseline.cagr}%、最大回檔 {r.backtest.baseline.max_dd}%、
          38 日平均 {r.backtest.baseline.avg38}%。代價：38 日收正機率 {r.backtest.adopted.pos38}%（固定配置 {r.backtest.baseline.pos38}%）。
          黃燈訊號（VIX&gt;25、費半前一晚跌&gt;3%、逼近季線且月線下彎）之後 20 日內跌逾 5% 的機率 31～41%（平常約 20%），但平均報酬沒有變差，所以只暫停追高、不急著賣。
        </p>
      )}
    </section>
  )
}
