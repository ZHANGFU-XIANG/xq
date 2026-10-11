import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import CsvImport from '../components/CsvImport'
import { Empty, ErrorNote, Loading, Section, Stat, StockLink } from '../components/ui'
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
  const amount = side === 'SELL' ? Math.round(shares * price) : Math.floor(shares * price + 1e-6)  // 同 App：買進捨去、賣出四捨五入
  const fee = shares ? Math.max(20, Math.ceil(amount * 0.001425 - 1e-9)) : 0  // 每筆無條件進位
  const tax = side === 'SELL' ? Math.floor(amount * (code.startsWith('00') ? 0.001 : 0.003) + 1e-9) : 0  // 證交稅元以下捨去
  return { amount, fee, tax }
}

type Kind = 'STOCK' | 'FUT' | 'OPT' | 'TRANSFER' | 'CASH'
const KINDS: [Kind, string][] = [['STOCK', '股票／ETF'], ['FUT', '期貨'], ['OPT', '選擇權'], ['TRANSFER', '資金調撥'], ['CASH', '手續費校正']]
const FUT_ROOTS: [string, string, number][] = [['TMF', '微台', 10], ['MTX', '小台', 50], ['TX', '大台', 200]]
const thisMonth = () => today().slice(0, 7)
const ymOf = (s: string) => `${s.slice(0, 4)}-${s.slice(4, 6)}`
const OPT_RE = /^TXO(\d{6})([CP])(\d+)$/

type Draft = {
  kind: Kind; date: string; side: 'BUY' | 'SELL'; code: string; lots: string; odd: string; qty: string; price: string
  margin: boolean; note: string; root: string; ym: string; cp: 'C' | 'P'; strike: string; dir: 'TO_FUTURES' | 'TO_SECURITIES'; amount: string
}

const blank = (): Draft => ({ kind: 'STOCK', date: today(), side: 'BUY', code: '', lots: '', odd: '', qty: '', price: '', margin: false,
  note: '', root: 'TMF', ym: thisMonth(), cp: 'P', strike: '', dir: 'TO_FUTURES', amount: '' })

/** 把一列紀錄轉回表單欄位（修改既有紀錄用） */
const fromRow = (r?: Json): Draft => {
  const d = blank()
  if (!r) return d
  const base = { ...d, date: r.date, note: r.note ?? '' }
  if (r.kind === 'TRANSFER') return { ...base, kind: 'TRANSFER', dir: r.side, amount: String(r.price) }
  if (r.kind === 'CASH') return { ...base, kind: 'CASH', amount: String(r.price) }
  if (r.kind === 'FUT') return { ...base, kind: 'FUT', side: r.side, root: r.code, ym: ymOf(r.contract.slice(r.code.length)), qty: String(r.shares), price: String(r.price) }
  if (r.kind === 'OPT') {
    const m = OPT_RE.exec(r.contract) ?? []
    return { ...base, kind: 'OPT', side: r.side, ym: m[1] ? ymOf(m[1]) : d.ym, cp: (m[2] as 'C' | 'P') ?? 'P', strike: m[3] ?? '', qty: String(r.shares), price: String(r.price) }
  }
  const sh = Number(r.shares)
  return { ...base, side: r.side, code: r.code, lots: String(Math.floor(sh / 1000) || ''), odd: String(sh % 1000 || ''),
    price: String(r.price), margin: r.margin === '1' }
}

/** 表單 → API 內容；欄位不完整回 null */
function toPayload(d: Draft): Json | null {
  const price = Number(d.price) || 0
  const qty = Number(d.qty) || 0
  const base = { date: d.date, note: d.note }
  if (!d.date) return null
  switch (d.kind) {
    case 'STOCK': {
      const shares = (Number(d.lots) || 0) * 1000 + (Number(d.odd) || 0)
      const code = d.code.trim().toUpperCase()
      if (!code || shares <= 0 || price <= 0) return null
      return { ...base, kind: 'STOCK', side: d.side, code, shares, price, margin: d.side === 'BUY' && d.margin }
    }
    case 'FUT':
      if (qty <= 0 || price <= 0 || !d.ym) return null
      return { ...base, kind: 'FUT', side: d.side, code: d.root, contract: `${d.root}${d.ym.replace('-', '')}`, shares: qty, price }
    case 'OPT':
      if (qty <= 0 || price <= 0 || !d.ym || !Number(d.strike)) return null
      return { ...base, kind: 'OPT', side: d.side, code: 'TXO', contract: `TXO${d.ym.replace('-', '')}${d.cp}${Number(d.strike)}`, shares: qty, price }
    case 'TRANSFER':
      return Number(d.amount) > 0 ? { ...base, kind: 'TRANSFER', side: d.dir, price: Number(d.amount) } : null
    case 'CASH':
      return Number(d.amount) ? { ...base, kind: 'CASH', price: Number(d.amount) } : null
  }
}

function TradeForm({ initial, holdings, derivs, busy, onSubmit, onCancel }: {
  initial?: Json; holdings: Json[]; derivs: Json[]; busy: boolean; onSubmit: (t: Json) => void; onCancel?: () => void
}) {
  const [d, setD] = useState<Draft>(() => fromRow(initial))
  const set = (patch: Partial<Draft>) => setD((x) => ({ ...x, ...patch }))
  const payload = toPayload(d)
  const shares = (Number(d.lots) || 0) * 1000 + (Number(d.odd) || 0)
  const price = Number(d.price) || 0
  const qty = Number(d.qty) || 0
  const held = holdings.find((h) => h.code === d.code.trim().toUpperCase())
  const est = estimate(d.code, d.side, shares, price)
  const mult = d.kind === 'OPT' ? 50 : FUT_ROOTS.find((r) => r[0] === d.root)?.[2] ?? 0
  const openDerivs = derivs.filter((x) => (d.kind === 'FUT' ? x.kind === 'FUT' : x.kind === 'OPT'))
  const input = 'rounded-md border border-line bg-paper px-2 py-1 text-ink outline-none focus:border-link'
  const field = 'flex flex-col gap-1 text-muted'
  const pill = (on: boolean, color = 'bg-ink text-paper') => `rounded-md px-3 py-1 ${on ? color : 'border border-line text-ink-2'}`
  const sideLabels = d.kind === 'FUT' ? ['買進（做多／平空）', '賣出（做空／平多）'] : d.kind === 'OPT' ? ['買進（建倉）', '賣出（平倉）'] : ['買進', '賣出']

  /** 從既有期貨／選擇權部位帶入平倉內容 */
  function closeDeriv(x: Json) {
    const c: string = x.contract
    if (x.kind === 'FUT') set({ root: x.root, ym: ymOf(c.slice(x.root.length)), side: x.qty > 0 ? 'SELL' : 'BUY', qty: String(Math.abs(x.qty)) })
    else {
      const m = OPT_RE.exec(c) ?? []
      set({ ym: m[1] ? ymOf(m[1]) : d.ym, cp: (m[2] as 'C' | 'P') ?? 'P', strike: m[3] ?? '', side: 'SELL', qty: String(x.qty) })
    }
  }

  return (
    <form className="rounded-lg border border-line bg-paper p-4 text-sm"
      onSubmit={(e) => { e.preventDefault(); if (payload) onSubmit(payload) }}>
      <div className="mb-3 flex flex-wrap gap-1.5" role="tablist" aria-label="類別">
        {KINDS.map(([k, label]) => (
          <button key={k} type="button" role="tab" aria-selected={d.kind === k} className={pill(d.kind === k)} onClick={() => set({ kind: k })}>{label}</button>
        ))}
      </div>

      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        {(d.kind === 'STOCK' || d.kind === 'FUT' || d.kind === 'OPT') && (
          <div className="flex gap-1.5" role="group" aria-label="買賣方向">
            <button type="button" className={pill(d.side === 'BUY', 'bg-up text-white')} onClick={() => set({ side: 'BUY' })}>{sideLabels[0]}</button>
            <button type="button" className={pill(d.side === 'SELL', 'bg-down text-white')} onClick={() => set({ side: 'SELL' })}>{sideLabels[1]}</button>
          </div>
        )}
        {d.kind === 'TRANSFER' && (
          <div className="flex gap-1.5" role="group" aria-label="調撥方向">
            <button type="button" className={pill(d.dir === 'TO_FUTURES')} onClick={() => set({ dir: 'TO_FUTURES' })}>證券戶 → 期貨戶</button>
            <button type="button" className={pill(d.dir === 'TO_SECURITIES')} onClick={() => set({ dir: 'TO_SECURITIES' })}>期貨戶 → 證券戶</button>
          </div>
        )}
        <label className={field}>日期
          <input type="date" value={d.date} max={today()} onChange={(e) => set({ date: e.target.value })} className={`${input} num`} required />
        </label>

        {d.kind === 'STOCK' && (
          <>
            <label className={field}>代號
              <input value={d.code} onChange={(e) => set({ code: e.target.value })} list="trade-holdings" placeholder="2330 或 0050"
                className={`${input} num w-28`} required />
              <datalist id="trade-holdings">
                {holdings.map((h) => <option key={h.code} value={h.code}>{h.name}（持有 {fmtInt(h.shares)} 股）</option>)}
              </datalist>
            </label>
            <label className={field}>張
              <input type="number" min={0} step={1} value={d.lots} onChange={(e) => set({ lots: e.target.value })} className={`${input} num w-20`} />
            </label>
            <label className={field}>零股（股）
              <input type="number" min={0} max={999} step={1} value={d.odd} onChange={(e) => set({ odd: e.target.value })} className={`${input} num w-20`} />
            </label>
            <label className={field}>成交價
              <input type="number" min={0} step="any" value={d.price} onChange={(e) => set({ price: e.target.value })} className={`${input} num w-24`} required />
            </label>
            {d.side === 'BUY' && (
              <label className="inline-flex items-center gap-1.5 pb-1 text-ink-2">
                <input type="checkbox" checked={d.margin} onChange={(e) => set({ margin: e.target.checked })} /> 融資
              </label>
            )}
          </>
        )}

        {(d.kind === 'FUT' || d.kind === 'OPT') && (
          <>
            {d.kind === 'FUT' ? (
              <label className={field}>商品
                <select value={d.root} onChange={(e) => set({ root: e.target.value })} className={input}>
                  {FUT_ROOTS.map(([r, n, m]) => <option key={r} value={r}>{r} {n}（每點 {m} 元）</option>)}
                </select>
              </label>
            ) : (
              <>
                <div className="flex gap-1.5" role="group" aria-label="買權或賣權">
                  <button type="button" className={pill(d.cp === 'P')} onClick={() => set({ cp: 'P' })}>賣權 P</button>
                  <button type="button" className={pill(d.cp === 'C')} onClick={() => set({ cp: 'C' })}>買權 C</button>
                </div>
                <label className={field}>履約價
                  <input type="number" min={0} step={50} value={d.strike} onChange={(e) => set({ strike: e.target.value })} className={`${input} num w-24`} required />
                </label>
              </>
            )}
            <label className={field}>合約月份
              <input type="month" value={d.ym} onChange={(e) => set({ ym: e.target.value })} className={`${input} num`} required />
            </label>
            <label className={field}>口數
              <input type="number" min={1} step={1} value={d.qty} onChange={(e) => set({ qty: e.target.value })} className={`${input} num w-20`} required />
            </label>
            <label className={field}>{d.kind === 'FUT' ? '成交點數' : '權利金（點）'}
              <input type="number" min={0} step="any" value={d.price} onChange={(e) => set({ price: e.target.value })} className={`${input} num w-28`} required />
            </label>
          </>
        )}

        {(d.kind === 'TRANSFER' || d.kind === 'CASH') && (
          <label className={field}>{d.kind === 'TRANSFER' ? '金額（元）' : '校正金額（元，正數＝多扣、負數＝退回）'}
            <input type="number" step="any" value={d.amount} onChange={(e) => set({ amount: e.target.value })} className={`${input} num w-36`} required />
          </label>
        )}

        <label className="flex min-w-40 flex-1 flex-col gap-1 text-muted">備註（理由）
          <input value={d.note} onChange={(e) => set({ note: e.target.value })} maxLength={80}
            placeholder={d.kind === 'CASH' ? '例如：依 App 淨收付校正' : '例如：跌破停損、換股、避險'} className={input} />
        </label>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        {d.kind === 'STOCK' && held && d.side === 'SELL' && (
          <button type="button" className="rounded-md border border-line px-2 py-0.5 text-ink-2"
            onClick={() => set({ lots: String(Math.floor(held.shares / 1000) || ''), odd: String(held.shares % 1000 || '') })}>
            全部賣出（{qtyText(held.shares)}）
          </button>
        )}
        {d.kind === 'STOCK' && held && <span className="text-muted">{held.name} 目前持有 {qtyText(held.shares)}</span>}
        {d.kind === 'STOCK' && shares > 0 && price > 0 && (
          <span className="num text-ink-2">
            {qtyText(shares)}・價金 {fmtInt(est.amount)}・手續費約 {fmtInt(est.fee)}{d.side === 'SELL' ? `・證交稅約 ${fmtInt(est.tax)}` : ''}
          </span>
        )}
        {(d.kind === 'FUT' || d.kind === 'OPT') && openDerivs.map((x) => (
          <button key={x.contract} type="button" className="rounded-md border border-line px-2 py-0.5 text-ink-2" onClick={() => closeDeriv(x)}>
            平倉 {x.contract}（{x.qty > 0 ? '多' : '空'} {Math.abs(x.qty)} 口，均價 {x.avg_price}）
          </button>
        ))}
        {(d.kind === 'FUT' || d.kind === 'OPT') && qty > 0 && price > 0 && (
          <span className="num text-ink-2">
            {d.kind === 'FUT' ? '契約價值' : '權利金'} {fmtInt(price * mult * qty)} 元（{price} 點 × {mult} 元 × {qty} 口）
          </span>
        )}
        {d.kind === 'OPT' && <span className="text-muted">競賽只做買方：賣出僅限平倉</span>}
      </div>
      <div className="mt-3 flex gap-2">
        <button type="submit" disabled={busy || !payload} className="rounded-md bg-ink px-4 py-1.5 text-paper disabled:opacity-40">
          {busy ? '寫入中…' : initial ? '儲存修改' : '新增紀錄'}
        </button>
        {onCancel && <button type="button" onClick={onCancel} className="rounded-md border border-line px-3 py-1.5">取消</button>}
      </div>
    </form>
  )
}

/** 交易紀錄的歷史版本（GitHub commit），可以整份復原；復原本身也是新版本，可以再還原回來。 */
function TradeHistory({ version, run, busy }: { version: string; run: (fn: () => Promise<Json>) => Promise<boolean>; busy: boolean }) {
  const [open, setOpen] = useState(false)
  const [pick, setPick] = useState<string | null>(null)
  const { data, isLoading, error } = useQuery<Json>({ queryKey: ['trades-history', version], queryFn: () => get('/api/trades/history'), enabled: open })
  if (!open) return <button className="rounded-md border border-line px-3 py-1.5 text-sm" onClick={() => setOpen(true)}>顯示歷史版本</button>
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  return (
    <ul className="divide-y divide-line/60 text-sm">
      {(data?.versions ?? []).map((v: Json, i: number) => (
        <li key={v.sha} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
          <span className="num text-muted">{new Date(v.date).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false })}</span>
          <span className="min-w-0 flex-1 truncate" title={v.message}>{v.message}</span>
          <span className="num text-ink-2">{v.rows ?? '—'} 筆・持股 {v.holdings ?? '—'} 檔</span>
          {v.oversized?.length > 0 && <span className="text-up" title={v.oversized.join('、')}>數量異常</span>}
          {i === 0 ? <span className="text-muted">目前版本</span> : pick === v.sha ? (
            <>
              <button disabled={busy} className="rounded bg-ink px-2 py-0.5 text-paper disabled:opacity-50"
                onClick={async () => { if (await run(() => post('/api/trades/restore', { version, sha: v.sha }))) setPick(null) }}>確定復原</button>
              <button className="text-muted" onClick={() => setPick(null)}>取消</button>
            </>
          ) : (
            <button disabled={v.oversized?.length > 0 || !v.rows} className="text-link disabled:text-muted" onClick={() => setPick(v.sha)}>復原成這個版本</button>
          )}
        </li>
      ))}
    </ul>
  )
}

const inSeg = (seg: Seg, kind: string) => seg === 'all' || (seg === 'sec' ? kind === 'STOCK' || kind === 'ETF' : kind === 'FUT' || kind === 'OPT')

function Ledger({ data, run, busy, seg }: { data: Json; run: (fn: () => Promise<Json>) => Promise<boolean>; busy: boolean; seg: Seg }) {
  const [editing, setEditing] = useState<number | null>(null)
  const [confirmDel, setConfirmDel] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [confirmBulk, setConfirmBulk] = useState(false)
  const [typed, setTyped] = useState('')
  const shown: Json[] = data.rows.filter((r: Json) => inSeg(seg, r.kind))
  if (!shown.length) return <Empty>{data.rows.length ? `沒有${SEG_LABEL[seg]}的紀錄。` : '還沒有任何交易紀錄。'}</Empty>
  const ids: number[] = shown.map((r: Json) => r.id)
  const live = new Set([...selected].filter((i) => ids.includes(i)))  // 重新整理後只保留仍存在的
  const allOn = live.size === ids.length
  const isStock = (r: Json) => r.kind === 'STOCK' || r.kind === 'ETF'
  const stockTotal = data.rows.filter(isStock).length
  const stockPicked = data.rows.filter((r: Json) => live.has(r.id) && isStock(r)).length
  const needType = stockTotal >= 4 && stockPicked * 2 >= stockTotal  // 與後端相同：刪掉一半以上股票紀錄要輸入確認
  const toggle = (id: number) => { setConfirmBulk(false); setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n }) }
  async function bulkDelete() {
    if (await run(() => post('/api/trades/bulk-delete', { version: data.version, ids: [...live], confirm: typed.trim() || undefined }))) {
      setSelected(new Set())
      setConfirmBulk(false)
    }
    setTyped('')
  }
  return (
    <div className="overflow-x-auto">
      <div className="mb-2 flex min-h-8 flex-wrap items-center gap-3 text-sm">
        <span className="text-muted">{live.size ? `已選 ${live.size} 筆` : '勾選左側方框可一次刪除多筆'}</span>
        {live.size > 0 && !confirmBulk && (
          <>
            <button className="rounded-md border border-up/50 px-3 py-1 text-up" onClick={() => setConfirmBulk(true)}>刪除選取的 {live.size} 筆</button>
            <button className="text-muted" onClick={() => setSelected(new Set())}>取消選取</button>
          </>
        )}
        {confirmBulk && (
          <>
            <span className="text-up">確定刪除 {live.size} 筆（其中股票 {stockPicked}／{stockTotal} 筆）？刪除後持股、現金、損益會重新計算</span>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="輸入「確認刪除」" aria-label="輸入確認刪除"
              className={`w-32 rounded border bg-paper px-2 py-0.5 text-ink ${needType ? 'border-up' : 'border-line'}`} />
            <button disabled={busy || (needType && typed.trim() !== '確認刪除')} className="rounded-md bg-up px-3 py-1 text-white disabled:opacity-50" onClick={bulkDelete}>{busy ? '刪除中…' : '確定刪除'}</button>
            <span className="w-full text-xs text-muted">會刪掉一半以上股票紀錄或清掉持股時，必須輸入「確認刪除」；刪錯可以在下方「歷史版本」復原</span>
            <button className="text-muted" onClick={() => { setConfirmBulk(false); setTyped('') }}>取消</button>
          </>
        )}
      </div>
      <table className="w-full min-w-[860px] text-[0.95rem]">
        <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
          <th className="w-8 py-2 font-normal">
            <input type="checkbox" aria-label="全選" checked={allOn}
              onChange={() => { setConfirmBulk(false); setSelected(allOn ? new Set() : new Set(ids)) }} />
          </th>
          <th className="py-2 font-normal">日期</th><th className="font-normal">買賣</th><th className="font-normal">標的</th>
          <th className="text-right font-normal">數量</th><th className="text-right font-normal">成交價</th><th className="text-right font-normal">價金</th>
          <th className="text-right font-normal">手續費＋稅</th><th className="pl-4 font-normal">備註</th><th className="text-right font-normal">操作</th>
        </tr></thead>
        <tbody>
          {shown.map((r: Json) => {
            const stock = r.kind === 'STOCK' || r.kind === 'ETF'
            return (
              <Fragment key={r.id}>
                <tr className={`border-b border-line/60 ${live.has(r.id) ? 'bg-up/5' : ''}`}>
                  <td className="py-1.5"><input type="checkbox" aria-label={`選取 ${r.date} ${r.code}`} checked={live.has(r.id)} onChange={() => toggle(r.id)} /></td>
                  <td className="num py-1.5 text-muted">{r.date}</td>
                  <td className={r.side === 'BUY' ? 'text-up' : r.side === 'SELL' ? 'text-down' : 'text-ink-2'}>{r.side_label}{r.margin === '1' ? '（融資）' : ''}</td>
                  <td>{stock ? <StockLink code={r.code} name={r.name} /> : <span className="text-ink-2">{r.contract || (r.kind === 'CASH' ? '證券戶' : '證券戶 ↔ 期貨戶')}{r.name ? <span className="ml-1 text-muted">{r.name}</span> : null}</span>}</td>
                  <td className="num text-right">{stock ? qtyText(Number(r.shares)) : r.kind === 'FUT' || r.kind === 'OPT' ? `${r.shares} 口` : '—'}</td>
                  <td className="num text-right">{stock || r.kind === 'FUT' || r.kind === 'OPT' ? r.price : '—'}</td>
                  <td className="num text-right">{r.amount != null ? fmtInt(r.amount) : fmtInt(Number(r.price))}</td>
                  <td className="num text-right text-muted">{r.fee != null ? fmtInt(r.fee + r.tax) : '—'}</td>
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
                        <button className="mr-3 text-link" onClick={() => { setEditing(editing === r.id ? null : r.id); setConfirmDel(null) }}>修改</button>
                        <button className="text-muted hover:text-up" onClick={() => { setConfirmDel(r.id); setEditing(null) }}>刪除</button>
                      </>
                    )}
                  </td>
                </tr>
                {editing === r.id && (
                  <tr><td colSpan={10} className="py-2">
                    <TradeForm initial={r} holdings={data.holdings} derivs={data.derivs} busy={busy} onCancel={() => setEditing(null)}
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


export type Seg = 'all' | 'sec' | 'fut'
export const SEG_LABEL: Record<Seg, string> = { all: '全部', sec: '證券（股票＋ETF）', fut: '期貨（期貨＋選擇權）' }

function SegmentSummary({ s, seg }: { s: Json; seg: Seg }) {
  const g = s.segments?.[seg]
  if (!g) return null
  return (
    <div className="mb-6 grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-5">
      <Stat label={`${SEG_LABEL[seg]}總損益`} value={fmtInt(g.total_pnl)} valueClass={tone(g.total_pnl)} sub="已實現＋未實現" />
      <Stat label="已實現" value={fmtInt(g.realized)} valueClass={tone(g.realized)} sub={seg === 'sec' ? '含手續費與證交稅' : seg === 'fut' ? '不含手續費（同 App）' : '證券＋期貨'} />
      <Stat label="未實現" value={fmtInt(g.unrealized)} valueClass={tone(g.unrealized)} sub={seg === 'fut' ? '期交所即時價' : '證券：市值 − 成交價金（同 App）'} />
      <Stat label="手續費＋稅" value={fmtInt(g.fees)} />
      <Stat label="交易筆數／標的" value={`${g.trade_count}／${g.symbols}`} />
    </div>
  )
}

function DerivStats({ d }: { d: Json }) {
  if (!d?.rows?.length) return <p className="mb-6 text-muted">還沒有期貨或選擇權紀錄。</p>
  return (
    <>
      <h3 className="mb-2 font-bold">期貨／選擇權各合約</h3>
      <div className="mb-6 overflow-x-auto">
        <table className="w-full min-w-[760px] text-[0.95rem]">
          <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
            <th className="py-2 font-normal">合約</th><th className="text-right font-normal">筆數</th><th className="text-right font-normal">未平倉</th>
            <th className="text-right font-normal">均價</th><th className="text-right font-normal">現價</th><th className="text-right font-normal">已實現</th>
            <th className="text-right font-normal">未實現</th><th className="text-right font-normal">合計</th><th className="text-right font-normal">費用</th>
          </tr></thead>
          <tbody>
            {d.rows.map((x: Json) => (
              <tr key={x.contract} className="border-b border-line/60">
                <td className="py-1.5"><span className="text-fut">{x.kind}</span> {x.contract}<span className="ml-1 text-xs text-muted">{x.name}</span></td>
                <td className="num text-right">{x.trades}</td>
                <td className="num text-right">{x.qty ? `${x.qty > 0 ? '多' : '空'} ${Math.abs(x.qty)} 口` : '已平倉'}</td>
                <td className="num text-right">{x.avg_price != null ? fmtNum(x.avg_price) : '—'}</td>
                <td className="num text-right">{x.mark != null ? fmtNum(x.mark) : '—'}</td>
                <td className={`num text-right ${tone(x.realized)}`}>{fmtInt(x.realized)}</td>
                <td className={`num text-right ${tone(x.unrealized)}`}>{x.qty ? fmtInt(x.unrealized) : '—'}</td>
                <td className={`num text-right font-medium ${tone(x.total_pnl)}`}>{fmtInt(x.total_pnl)}</td>
                <td className="num text-right text-muted">{fmtInt(x.fees)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function Stats({ s, seg }: { s: Json; seg: Seg }) {
  return (
    <>
      <SegmentSummary s={s} seg={seg} />
      {seg !== 'fut' && <SecStats s={s} />}
      {seg !== 'sec' && <DerivStats d={s.derivs} />}
    </>
  )
}

function SecStats({ s }: { s: Json }) {
  const m = s.summary
  return (
    <>
      <div className="mb-6 grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4 lg:grid-cols-6">
        <Stat label="勝率" value={m.win_rate == null ? '—' : `${m.win_rate}%`} sub={`賺 ${m.win_count} 次・賠 ${m.loss_count} 次`} />
        <Stat label="平均獲利／虧損" value={<span><span className="text-up">{fmtInt(m.avg_win)}</span>／<span className="text-down">{fmtInt(m.avg_loss)}</span></span>}
          sub={m.profit_factor != null ? `獲利因子 ${m.profit_factor}（> 1 代表賺的比賠的多）` : '獲利因子：需有賺有賠'} />
        <Stat label="平均持有" value={m.avg_hold_days == null ? '—' : `${m.avg_hold_days} 天`} sub="交易日，依股數加權" />
        <Stat label="交易筆數／標的" value={`${m.trade_count}／${m.symbols}`} sub={`買 ${m.buy_count}・賣 ${m.sell_count}・期權 ${m.deriv_trades}`} />
        <Stat label="買進金額" value={fmtInt(m.buy_amount)} />
        <Stat label="賣出金額" value={fmtInt(m.sell_amount)} />
        <Stat label="手續費" value={fmtInt(m.fee)} sub={m.fee_adjust ? `另有 App 校正 ${fmtInt(m.fee_adjust)} 元` : undefined} />
        {m.min_fee_extra > 0 && (
          <Stat label="最低手續費多付" value={fmtInt(m.min_fee_extra)}
            sub={`小額或零股分次成交每筆至少 20 元：${(m.min_fee_extra_top ?? []).join('、')}`} />
        )}
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

/** 交易紀錄區塊（嵌在「持股與交易」頁）：view = trades（新增／匯入／列表）或 stats（統計）；seg = 全部／證券／期貨 */
export default function TradeWorkspace({ view, seg }: { view: 'trades' | 'stats'; seg: Seg }) {
  const qc = useQueryClient()
  const { data, isLoading, error, refetch } = useQuery<Json>({ queryKey: ['trades'], queryFn: () => get('/api/trades') })
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [formKey, setFormKey] = useState(0)

  /** 執行寫入；成功後先更新本頁，其他資料（持股、建議、警示）在背景重算。 */
  async function run(fn: () => Promise<Json>): Promise<boolean> {
    setBusy(true)
    setMsg(null)
    try {
      const r = await fn()
      setMsg({ ok: true, text: r.message })
      await qc.invalidateQueries({ queryKey: ['trades'] })
      qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== 'trades' })
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
  if (view === 'stats')
    return <Section title={`統計與檢討・${SEG_LABEL[seg]}`} aside="數字由交易紀錄重播計算，賣出先進先出"><Stats s={data.stats} seg={seg} /></Section>
  return (
    <>
      {msg && <div className={`mb-4 rounded-md border px-3 py-2 text-sm ${msg.ok ? 'border-down/40 bg-down/5 text-down' : 'border-up/40 bg-up/5 text-up'}`}>{msg.text}</div>}
      <Section title="全部交易" aside={`${SEG_LABEL[seg]}・最新在前・可修改、刪除或多選刪除`}>
        <Ledger data={data} run={run} busy={busy} seg={seg} />
      </Section>

      <Section title="歷史版本" aside="每次寫入交易紀錄都會保存一個版本；刪錯或匯入錯誤時可以整份復原">
        <TradeHistory version={data.version} run={run} busy={busy} />
      </Section>

      <Section title="新增紀錄" aside="股票、ETF、期貨、選擇權、資金調撥、手續費校正；過去日期也能補記">
        <TradeForm key={formKey} holdings={data.holdings} derivs={data.derivs} busy={busy}
          onSubmit={async (t) => { if (await run(() => post('/api/trades', { version: data.version, trade: t }))) setFormKey((k) => k + 1) }} />
        <p className="mt-2 text-xs text-muted">
          寫入前會重算整個帳戶（賣超過持有會被擋下）；雲端版會提交到 GitHub，所有頁面的持股、現金、建議都會跟著更新。
          記錯了可以在「全部交易」按「修改」更正，或刪除後重記。
        </p>
      </Section>

      <Section title="匯入 XQ CSV" aside="委託回報或成交回報匯出的 CSV，已記錄過的自動標示">
        <CsvImport busy={busy} run={run} />
      </Section>
    </>
  )
}
