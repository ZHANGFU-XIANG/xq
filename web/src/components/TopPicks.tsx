import { Fragment, useState } from 'react'
import { Change, Empty, StockLink } from './ui'
import type { Json } from '../lib/api'
import { fmtInt, fmtPct } from '../lib/format'

const pct = (v: number | null | undefined, d = 1, sign = true) => (v == null ? '—' : fmtPct(v * 100, d, sign))
const VERDICT: Record<string, string> = { 可以買: 'bg-up text-white', 等回檔再買: 'bg-warn/15 text-warn', 先不要買: 'bg-mist text-muted' }
const CONF: Record<string, string> = { 高: 'text-down', 中: 'text-ink-2', 低: 'text-warn' }

/** 綜合排名表：綜合預期 20 日報酬＝模型＋資訊事件×消息可信度－美股重挫影響（不參考持股）。點列展開組成與理由。 */
export default function TopPicks({ items }: { items: Json[] }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!items.length) return <Empty>沒有符合條件的股票</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[860px] text-[0.95rem]">
        <thead className="text-left text-sm text-muted">
          <tr className="border-b border-line">
            <th className="whitespace-nowrap py-2 font-normal">排名</th><th className="font-normal">股票</th><th className="font-normal">結論</th>
            <th className="text-right font-normal">現價</th><th className="text-right font-normal">漲跌</th>
            <th className="text-right font-normal">綜合預期</th><th className="text-right font-normal">勝率</th>
            <th className="text-right font-normal">可信度</th><th className="text-right font-normal">預期回檔</th>
            <th className="text-right font-normal">停損／目標</th><th className="text-right font-normal">一張</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <Fragment key={r.code}>
              <tr className="cursor-pointer border-b border-line/60 hover:bg-mist" onClick={() => setOpen(open === r.code ? null : r.code)}>
                <td className="num py-2 text-muted">{r.rank}</td>
                <td>
                  <StockLink code={r.code} name={r.name} />
                  <span className="ml-1.5 text-xs text-muted">{r.sector}</span>
                  {r.event && <span className="ml-1.5 whitespace-nowrap rounded bg-up/10 px-1 text-xs text-up">{r.event.label}</span>}
                  {r.attention && <span className="ml-1.5 whitespace-nowrap rounded bg-warn/15 px-1 text-xs text-warn">{r.attention}股</span>}
                </td>
                <td><span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${VERDICT[r.verdict]}`}>{r.verdict}</span></td>
                <td className="num text-right">{r.price}</td>
                <td className="text-right"><Change value={r.chg_pct} /></td>
                <td className="num text-right font-bold text-up">{fmtPct(r.score, 1)}</td>
                <td className="num text-right">{pct(r.p_up, 0, false)}</td>
                <td className={`text-right text-sm font-medium ${CONF[r.confidence]}`}>{r.confidence}</td>
                <td className="num text-right text-down">{pct(r.exp_dd)}</td>
                <td className="num whitespace-nowrap text-right text-sm"><span className="text-down">{r.stop ?? '—'}</span>／<span className="text-up">{r.target}</span></td>
                <td className="num text-right text-sm">{fmtInt(r.lot_cost)}</td>
              </tr>
              {open === r.code && (
                <tr className="border-b border-line/60 bg-mist/60">
                  <td />
                  <td colSpan={10} className="py-2 pr-2 text-sm leading-relaxed text-ink-2">
                    <div className="mb-1 font-medium text-ink">買點：{r.entry_note}</div>
                    <div className="mb-1 font-medium text-ink">出場：{r.exit.text}</div>
                    <div className="mb-1">綜合預期 {fmtPct(r.score, 1)} ＝ {r.parts.map((p: Json) => `${p.name} ${fmtPct(p.pct, 1)}（${p.note}）`).join(' ＋ ')}</div>
                    {r.event?.news?.length > 0 && <div className="mb-1">相關消息：{r.event.news.join('；')}</div>}
                    <ul className="list-disc space-y-0.5 pl-4">{r.reasons.map((t: string, i: number) => <li key={i}>{t}</li>)}</ul>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">點一下列看組成與理由；勝率＝模型估計 20 日收正機率；可信度看勝率與消息來源（正式公告／多來源為高，單一來源、查無消息或公司澄清為低）。</p>
    </div>
  )
}
