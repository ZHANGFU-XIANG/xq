import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { Empty, ErrorNote, Loading, PageTitle, Section, Stat, StockLink } from '../components/ui'
import { ApiError, del, get, post, put, type Json } from '../lib/api'
import { fmtInt, fmtNum, fmtPct, tone } from '../lib/format'

const today = () => new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Taipei' })

/** 股數 → 「2 張 + 345 股」 */
const qtyText = (shares: number) => {
  const lots = Math.floor(shares / 1000)
  const odd = shares % 1000
  return `${lots ? `${lots} 張` : ''}${lots && odd ? ' + ' : ''}${odd ? `${odd} 股` : ''}` || '0 股'
}

/** 前端預估（與後端相同公式：手續費 0.1425%、最低 20 元；證交稅股票 0.3%、ETF 0.1%，只有賣出收） */
function estimate(code: string, side: string, shares: number, price: number) {
  const amount = shares * price
  const fee = shares ? Math.max(20, Math.round(amount * 0.001425)) : 0
  const tax = side === 'SELL' ? Math.round(amount * (code.startsWith('00') ? 0.001 : 0.003)) : 0
  return { amount, fee, tax }
}

type Draft = { date: string; side: 'BUY' | 'SELL'; code: string; lots: string; odd: string; price: string; margin: boolean; note: string }

const fromRow = (r?: Json): Draft => {
  if (!r) return { date: today(), side: 'BUY', code: '', lots: '', odd: '', price: '', margin: false, note: '' }
  const sh = Number(r.shares)
  return { date: r.date, side: r.side, code: r.code, lots: String(Math.floor(sh / 1000) || ''), odd: String(sh % 1000 || ''),
    price: String(r.price), margin: r.margin === '1', note: r.note ?? '' }
}

function TradeForm({ initial, holdings, busy, onSubmit, onCancel }: {
  initial?: Json; holdings: Json[]; busy: boolean; onSubmit: (t: Json) => void; onCancel?: () => void
}) {
  const [d, setD] = useState<Draft>(() => fromRow(initial))
  const set = (patch: Partial<Draft>) => setD((x) => ({ ...x, ...patch }))
  const shares = (Number(d.lots) || 0) * 1000 + (Number(d.odd) || 0)
  const price = Number(d.price) || 0
  const held = holdings.find((h) => h.code === d.code.trim().toUpperCase())
  const est = estimate(d.code, d.side, shares, price)
  const valid = d.code.trim() && shares > 0 && price > 0 && d.date
  const input = 'rounded-md border border-line bg-paper px-2 py-1 text-ink outline-none focus:border-link'
  const sideBtn = (s: 'BUY' | 'SELL') =>
    `rounded-md px-3 py-1 ${d.side === s ? (s === 'BUY' ? 'bg-up text-white' : 'bg-down text-white') : 'border border-line text-ink-2'}`
  return (
    <form className="rounded-lg border border-line bg-paper p-4 text-sm"
      onSubmit={(e) => {
        e.preventDefault()
        if (valid) onSubmit({ date: d.date, side: d.side, code: d.code.trim().toUpperCase(), shares, price, margin: d.side === 'BUY' && d.margin, note: d.note })
      }}>
      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        <div className="flex gap-1.5" role="group" aria-label="買賣方向">
          <button type="button" className={sideBtn('BUY')} onClick={() => set({ side: 'BUY' })}>買進</button>
          <button type="button" className={sideBtn('SELL')} onClick={() => set({ side: 'SELL' })}>賣出</button>
        </div>
        <label className="flex flex-col gap-1 text-muted">日期
          <input type="date" value={d.date} max={today()} onChange={(e) => set({ date: e.target.value })} className={`${input} num`} required />
        </label>
        <label className="flex flex-col gap-1 text-muted">代號
          <input value={d.code} onChange={(e) => set({ code: e.target.value })} list="trade-holdings" placeholder="2330"
            className={`${input} num w-24`} required />
          <datalist id="trade-holdings">
            {holdings.map((h) => <option key={h.code} value={h.code}>{h.name}（持有 {fmtInt(h.shares)} 股）</option>)}
          </datalist>
        </label>
        <label className="flex flex-col gap-1 text-muted">張
          <input type="number" min={0} step={1} value={d.lots} onChange={(e) => set({ lots: e.target.value })} className={`${input} num w-20`} />
        </label>
        <label className="flex flex-col gap-1 text-muted">零股（股）
          <input type="number" min={0} max={999} step={1} value={d.odd} onChange={(e) => set({ odd: e.target.value })} className={`${input} num w-20`} />
        </label>
        <label className="flex flex-col gap-1 text-muted">成交價
          <input type="number" min={0} step="any" value={d.price} onChange={(e) => set({ price: e.target.value })} className={`${input} num w-24`} required />
        </label>
        {d.side === 'BUY' && (
          <label className="inline-flex items-center gap-1.5 pb-1 text-ink-2">
            <input type="checkbox" checked={d.margin} onChange={(e) => set({ margin: e.target.checked })} /> 融資
          </label>
        )}
        <label className="flex min-w-40 flex-1 flex-col gap-1 text-muted">備註（理由）
          <input value={d.note} onChange={(e) => set({ note: e.target.value })} maxLength={80} placeholder="例如：跌破停損、換股" className={input} />
        </label>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        {held && d.side === 'SELL' && (
          <button type="button" className="rounded-md border border-line px-2 py-0.5 text-ink-2"
            onClick={() => set({ lots: String(Math.floor(held.shares / 1000) || ''), odd: String(held.shares % 1000 || '') })}>
            全部賣出（{qtyText(held.shares)}）
          </button>
        )}
        {held && <span className="text-muted">{held.name} 目前持有 {qtyText(held.shares)}</span>}
        {shares > 0 && price > 0 && (
          <span className="num text-ink-2">
            {qtyText(shares)}・價金 {fmtInt(est.amount)}・手續費約 {fmtInt(est.fee)}{d.side === 'SELL' ? `・證交稅約 ${fmtInt(est.tax)}` : ''}
          </span>
        )}
      </div>
      <div className="mt-3 flex gap-2">
        <button type="submit" disabled={busy || !valid} className="rounded-md bg-ink px-4 py-1.5 text-paper disabled:opacity-40">
          {busy ? '寫入中…' : initial ? '儲存修改' : '新增交易'}
        </button>
        {onCancel && <button type="button" onClick={onCancel} className="rounded-md border border-line px-3 py-1.5">取消</button>}
      </div>
    </form>
  )
}

function Ledger({ data, run, busy }: { data: Json; run: (fn: () => Promise<Json>) => Promise<boolean>; busy: boolean }) {
  const [editing, setEditing] = useState<number | null>(null)
  const [confirmDel, setConfirmDel] = useState<number | null>(null)
  if (!data.rows.length) return <Empty>還沒有任何交易紀錄。</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[820px] text-[0.95rem]">
        <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
          <th className="py-2 font-normal">日期</th><th className="font-normal">買賣</th><th className="font-normal">標的</th>
          <th className="text-right font-normal">數量</th><th className="text-right font-normal">成交價</th><th className="text-right font-normal">價金</th>
          <th className="text-right font-normal">手續費＋稅</th><th className="pl-4 font-normal">備註</th><th className="text-right font-normal">操作</th>
        </tr></thead>
        <tbody>
          {data.rows.map((r: Json) => {
            const stock = r.kind === 'STOCK' || r.kind === 'ETF'
            return (
              <Fragment key={r.id}>
                <tr className="border-b border-line/60">
                  <td className="num py-1.5 text-muted">{r.date}</td>
                  <td className={r.side === 'BUY' ? 'text-up' : r.side === 'SELL' ? 'text-down' : 'text-ink-2'}>{r.side_label}{r.margin === '1' ? '（融資）' : ''}</td>
                  <td>{stock ? <StockLink code={r.code} name={r.name} /> : <span className="text-ink-2">{r.contract || r.code}</span>}</td>
                  <td className="num text-right">{stock ? qtyText(Number(r.shares)) : r.kind === 'FUT' || r.kind === 'OPT' ? `${r.shares} 口` : '—'}</td>
                  <td className="num text-right">{stock || r.kind === 'FUT' || r.kind === 'OPT' ? r.price : '—'}</td>
                  <td className="num text-right">{stock ? fmtInt(r.amount) : r.kind === 'CASH' || r.kind === 'TRANSFER' ? fmtInt(Number(r.price)) : '—'}</td>
                  <td className="num text-right text-muted">{stock ? fmtInt(r.fee + r.tax) : '—'}</td>
                  <td className="max-w-64 truncate pl-4 text-sm text-muted" title={r.note}>{r.note}</td>
                  <td className="whitespace-nowrap text-right text-sm">
                    {confirmDel === r.id ? (
                      <>
                        <button disabled={busy} className="mr-2 rounded bg-up px-2 py-0.5 text-white disabled:opacity-50"
                          onClick={async () => { if (await run(() => del(`/api/trades/${r.id}?version=${data.version}`))) setConfirmDel(null) }}>確定刪除</button>
                        <button className="text-muted" onClick={() => setConfirmDel(null)}>取消</button>
                      </>
                    ) : (
                      <>
                        {stock && <button className="mr-3 text-link" onClick={() => { setEditing(editing === r.id ? null : r.id); setConfirmDel(null) }}>修改</button>}
                        <button className="text-muted hover:text-up" onClick={() => { setConfirmDel(r.id); setEditing(null) }}>刪除</button>
                      </>
                    )}
                  </td>
                </tr>
                {editing === r.id && (
                  <tr><td colSpan={9} className="py-2">
                    <TradeForm initial={r} holdings={data.holdings} busy={busy} onCancel={() => setEditing(null)}
                      onSubmit={async (t) => { if (await run(() => put(`/api/trades/${r.id}`, { version: data.version, trade: t }))) setEditing(null) }} />
                  </td></tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Stats({ s }: { s: Json }) {
  const m = s.summary
  return (
    <>
      <div className="mb-6 grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4 lg:grid-cols-6">
        <Stat label="總損益（已實現＋未實現）" value={fmtInt(m.total_pnl)} valueClass={tone(m.total_pnl)} sub="已實現含手續費與稅" />
        <Stat label="已實現損益" value={fmtInt(m.realized)} valueClass={tone(m.realized)} sub={`${m.closed_count} 次賣出`} />
        <Stat label="未實現損益" value={fmtInt(m.unrealized)} valueClass={tone(m.unrealized)} sub="市值 − 成交價金（同 App）" />
        <Stat label="勝率" value={m.win_rate == null ? '—' : `${m.win_rate}%`} sub={`賺 ${m.win_count} 次・賠 ${m.loss_count} 次`} />
        <Stat label="平均獲利／虧損" value={<span><span className="text-up">{fmtInt(m.avg_win)}</span>／<span className="text-down">{fmtInt(m.avg_loss)}</span></span>}
          sub={m.profit_factor != null ? `獲利因子 ${m.profit_factor}（> 1 代表賺的比賠的多）` : '獲利因子：需有賺有賠'} />
        <Stat label="平均持有" value={m.avg_hold_days == null ? '—' : `${m.avg_hold_days} 天`} sub="交易日，依股數加權" />
        <Stat label="交易筆數／標的" value={`${m.trade_count}／${m.symbols}`} sub={`買 ${m.buy_count}・賣 ${m.sell_count}・期權 ${m.deriv_trades}`} />
        <Stat label="買進金額" value={fmtInt(m.buy_amount)} />
        <Stat label="賣出金額" value={fmtInt(m.sell_amount)} />
        <Stat label="手續費" value={fmtInt(m.fee)} sub={m.fee_adjust ? `另有 App 校正 ${fmtInt(m.fee_adjust)} 元` : undefined} />
        <Stat label="證交稅" value={fmtInt(m.tax)} />
        <Stat label="交易天數" value={m.active_days} />
      </div>
      <ul className="mb-6 space-y-1 text-sm">
        {m.checks.map((c: Json) => (
          <li key={c.item} className={c.ok ? 'text-down' : 'text-warn'}>{c.ok ? '✓' : '✗'} {c.item}：目前 {c.value}</li>
        ))}
      </ul>

      <h3 className="mb-2 font-bold">各檔統計</h3>
      <div className="mb-6 overflow-x-auto">
        <table className="w-full min-w-[820px] text-[0.95rem]">
          <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
            <th className="py-2 font-normal">標的</th><th className="text-right font-normal">買／賣次數</th><th className="text-right font-normal">均買價</th>
            <th className="text-right font-normal">均賣價</th><th className="text-right font-normal">目前持有</th><th className="text-right font-normal">已實現</th>
            <th className="text-right font-normal">未實現</th><th className="text-right font-normal">合計</th><th className="text-right font-normal">費用＋稅</th>
          </tr></thead>
          <tbody>
            {s.per_symbol.map((x: Json) => (
              <tr key={x.code} className="border-b border-line/60">
                <td className="py-1.5"><StockLink code={x.code} name={x.name} /></td>
                <td className="num text-right">{x.buys}／{x.sells}</td>
                <td className="num text-right">{fmtNum(x.avg_buy)}</td>
                <td className="num text-right">{x.avg_sell == null ? '—' : fmtNum(x.avg_sell)}</td>
                <td className="num text-right">{x.held_shares ? qtyText(x.held_shares) : '已出清'}</td>
                <td className={`num text-right ${tone(x.realized)}`}>{x.sells ? fmtInt(x.realized) : '—'}</td>
                <td className={`num text-right ${tone(x.unrealized)}`}>{x.held_shares ? fmtInt(x.unrealized) : '—'}</td>
                <td className={`num text-right font-medium ${tone(x.total_pnl)}`}>{fmtInt(x.total_pnl)}</td>
                <td className="num text-right text-muted">{fmtInt(x.fee + x.tax)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3 className="mb-2 font-bold">已實現交易（每次賣出，先進先出對應買進）</h3>
      {!s.closed.length ? <p className="mb-6 text-muted">還沒有賣出紀錄。賣出後這裡會列出每一筆的損益、報酬率與持有天數。</p> : (
        <div className="mb-6 overflow-x-auto">
          <table className="w-full min-w-[760px] text-[0.95rem]">
            <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
              <th className="py-2 font-normal">賣出日</th><th className="font-normal">標的</th><th className="text-right font-normal">股數</th>
              <th className="text-right font-normal">買進價</th><th className="text-right font-normal">賣出價</th><th className="text-right font-normal">持有</th>
              <th className="text-right font-normal">損益</th><th className="text-right font-normal">報酬率</th><th className="pl-4 font-normal">備註</th>
            </tr></thead>
            <tbody>
              {s.closed.map((c: Json, i: number) => (
                <tr key={i} className="border-b border-line/60">
                  <td className="num py-1.5 text-muted">{c.sell_date}</td>
                  <td><StockLink code={c.code} name={c.name} /></td>
                  <td className="num text-right">{fmtInt(c.shares)}</td>
                  <td className="num text-right">{fmtNum(c.buy_price)}</td>
                  <td className="num text-right">{fmtNum(c.sell_price)}</td>
                  <td className="num text-right">{c.hold_days} 天</td>
                  <td className={`num text-right ${tone(c.pnl)}`}>{fmtInt(c.pnl)}</td>
                  <td className={`num text-right ${tone(c.ret_pct)}`}>{fmtPct(c.ret_pct)}</td>
                  <td className="max-w-56 truncate pl-4 text-sm text-muted" title={c.note}>{c.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="mb-2 font-bold">每日交易</h3>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-[0.95rem]">
          <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
            <th className="py-2 font-normal">日期</th><th className="text-right font-normal">筆數</th><th className="text-right font-normal">買進金額</th>
            <th className="text-right font-normal">賣出金額</th><th className="text-right font-normal">已實現</th><th className="text-right font-normal">費用＋稅</th>
          </tr></thead>
          <tbody>
            {s.daily.map((d: Json) => (
              <tr key={d.date} className="border-b border-line/60">
                <td className="num py-1.5 text-muted">{d.date}</td>
                <td className="num text-right">{d.count}</td>
                <td className="num text-right">{fmtInt(d.buy_amount)}</td>
                <td className="num text-right">{fmtInt(d.sell_amount)}</td>
                <td className={`num text-right ${tone(d.realized)}`}>{d.sell_amount ? fmtInt(d.realized) : '—'}</td>
                <td className="num text-right text-muted">{fmtInt(d.fee_tax)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

export default function Trades() {
  const qc = useQueryClient()
  const { data, isLoading, error, refetch } = useQuery<Json>({ queryKey: ['trades'], queryFn: () => get('/api/trades') })
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [formKey, setFormKey] = useState(0)

  /** 執行寫入；成功後重新整理所有資料（持股、建議、警示都依交易紀錄計算）。 */
  async function run(fn: () => Promise<Json>): Promise<boolean> {
    setBusy(true)
    setMsg(null)
    try {
      const r = await fn()
      setMsg({ ok: true, text: r.message })
      await qc.invalidateQueries()
      return true
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : '寫入失敗' })
      if (e instanceof ApiError && e.status === 409) refetch()
      return false
    } finally {
      setBusy(false)
    }
  }

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  return (
    <>
      <PageTitle>交易紀錄與統計</PageTitle>
      {msg && <div className={`mb-4 rounded-md border px-3 py-2 text-sm ${msg.ok ? 'border-down/40 bg-down/5 text-down' : 'border-up/40 bg-up/5 text-up'}`}>{msg.text}</div>}

      <Section title="手動新增交易" aside="買進、賣出都可記錄；整股填張數，零股填股數">
        <TradeForm key={formKey} holdings={data.holdings} busy={busy}
          onSubmit={async (t) => { if (await run(() => post('/api/trades', { version: data.version, trade: t }))) setFormKey((k) => k + 1) }} />
        <p className="mt-2 text-xs text-muted">
          寫入前會重算整個帳戶（賣超過持有會被擋下）；雲端版會提交到 GitHub，所有頁面的持股、現金、建議都會跟著更新。
          期貨／選擇權與資金調撥請跟 AI 助理說，或用持股頁的 XQ 匯入。
        </p>
      </Section>

      <Section title="全部交易" aside={`${data.rows.length} 筆・最新在前・可修改或刪除`}>
        <Ledger data={data} run={run} busy={busy} />
      </Section>

      <Section title="統計與檢討" aside="數字由交易紀錄重播計算，賣出先進先出">
        <Stats s={data.stats} />
      </Section>
    </>
  )
}
