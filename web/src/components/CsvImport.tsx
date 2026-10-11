import { useRef, useState } from 'react'
import { post, type Json } from '../lib/api'
import { fmtInt } from '../lib/format'

type Row = Json & { include: boolean; priceText: string; root: string; ymText: string; dateText: string }

const FUT_NAMES: Record<string, string> = { TX: '大台', MTX: '小台', TMF: '微台' }
const ymInput = (ym: string) => (ym ? `${ym.slice(0, 4)}-${ym.slice(4, 6)}` : '')

/** 讀檔 → base64（XQ 匯出為 Big5，交給後端解碼） */
async function toBase64(f: File): Promise<string> {
  const buf = new Uint8Array(await f.arrayBuffer())
  let bin = ''
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return btoa(bin)
}

/** XQ 委託／成交回報 CSV 匯入：解析 → 標示已記錄過的 → 可改價格、勾選 → 一次寫入。 */
export default function CsvImport({ busy, run }: { busy: boolean; run: (fn: () => Promise<Json>) => Promise<boolean> }) {
  const [res, setRes] = useState<Json | null>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const [fileName, setFileName] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const [allDate, setAllDate] = useState('')

  async function pick(list: FileList | File[] | null | undefined) {
    const fs = [...(list ?? [])]
    if (!fs.length) return
    setErr('')
    setRes(null)
    setRows([])
    setFileName(fs.map((f) => f.name).join('、'))
    setLoading(true)
    try {
      const r = await post('/api/xq/csv', { files: await Promise.all(fs.map(toBase64)) })
      if (r.error) setErr(r.error)
      setRes(r)
      setRows((r.rows ?? []).map((x: Json) => ({ ...x, include: !x.duplicate, priceText: String(x.price), root: x.code,
        ymText: ymInput(x.ym ?? ''), dateText: x.date ?? '' })))
    } catch (e) {
      setErr(e instanceof Error ? e.message : '讀取失敗')
    } finally {
      setLoading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const chosen = rows.filter((r) => r.include)
  const badPrice = chosen.some((r) => !(Number(r.priceText) > 0) || (r.kind === 'FUT' && !r.ymText) || !r.dateText)
  const missingDate = rows.some((r) => !r.date)
  const update = (i: number, patch: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  async function apply() {
    const trades = chosen.map((r) => {
      const note = `XQ CSV 匯入・${r.board}${Number(r.priceText) === r.price && r.price_source === '委託價' ? '・委託價' : ''}${r.order_no ? `・委託書號 ${r.order_no}` : ''}`
      return r.kind === 'FUT'
        ? { kind: 'FUT', date: r.dateText, code: r.root, contract: `${r.root}${r.ymText.replace('-', '')}`, side: r.side, shares: r.shares, price: Number(r.priceText), note }
        : { kind: 'STOCK', date: r.dateText, code: r.code, side: r.side, shares: r.shares, price: Number(r.priceText), margin: r.margin, note }
    })
    if (await run(() => post('/api/trades/bulk', { version: res!.version, trades }))) {
      setRes(null)
      setRows([])
      setFileName('')
    }
  }

  return (
    <div className="rounded-lg border border-line bg-paper p-4 text-sm">
      <ol className="mb-3 list-decimal space-y-0.5 pl-5 text-ink-2">
        <li>在 XQ 匯出「成交回報」CSV（實際成交價，最精確；證券、期貨都可以）</li>
        <li>可一次選多個檔案：成交回報只有時間沒有日期，同時選「委託回報」CSV 就能用委託書號對出日期</li>
        <li>已經記錄過的成交會自動標示、預設不勾選；核對後按「匯入」</li>
      </ol>
      <div className="flex min-h-20 cursor-pointer items-center justify-center rounded-md border-2 border-dashed border-line px-3 py-3 text-muted"
        onClick={() => fileRef.current?.click()} onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files) }}>
        {loading ? '讀取中…' : fileName ? `已選：${fileName}（點這裡換檔案）` : '點這裡選擇 CSV 檔（可多選），或拖曳檔案到這裡'}
      </div>
      <input ref={fileRef} type="file" multiple accept=".csv,text/csv" className="hidden" onChange={(e) => pick(e.target.files)} />
      {missingDate && rows.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-warn">有成交沒有日期：</span>
          <input type="date" value={allDate} onChange={(e) => setAllDate(e.target.value)} aria-label="套用日期"
            className="num rounded border border-line bg-paper px-1.5 py-0.5 text-ink" />
          <button type="button" disabled={!allDate} className="rounded-md border border-line px-2 py-0.5 disabled:opacity-40"
            onClick={() => setRows((rs) => rs.map((r) => (r.date ? r : { ...r, dateText: allDate })))}>套用到所有沒有日期的列</button>
        </div>
      )}

      {err && <p className="mt-2 text-up">{err}</p>}
      {res?.warnings?.length > 0 && <ul className="mt-2 space-y-0.5 text-warn">{res.warnings.map((w: string) => <li key={w}>{w}</li>)}</ul>}

      {rows.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[720px] text-[0.95rem]">
            <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
              <th className="w-8 py-1 font-normal">
                <input type="checkbox" aria-label="全選" checked={rows.every((r) => r.include)}
                  onChange={(e) => setRows((rs) => rs.map((r) => ({ ...r, include: e.target.checked })))} />
              </th>
              <th className="font-normal">日期</th><th className="font-normal">買賣</th><th className="font-normal">標的</th>
              <th className="text-right font-normal">數量</th><th className="text-right font-normal">價格</th><th className="pl-3 font-normal">狀態</th>
            </tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className={`border-b border-line/60 ${r.duplicate && !r.include ? 'text-muted' : ''}`}>
                  <td className="py-1.5"><input type="checkbox" aria-label={`匯入 ${r.code}`} checked={r.include} onChange={(e) => update(i, { include: e.target.checked })} /></td>
                  <td className="num">{r.date ? r.date : (
                    <input type="date" value={r.dateText} aria-label="成交日期" onChange={(e) => update(i, { dateText: e.target.value })}
                      className="num rounded border border-warn bg-paper px-1 py-0.5 text-ink" />)}</td>
                  <td className={r.side === 'BUY' ? 'text-up' : 'text-down'}>{r.side === 'BUY' ? '買進' : '賣出'}{r.margin ? '（融資）' : ''}</td>
                  <td>
                    {r.kind === 'FUT' ? (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <select value={r.root} aria-label="期貨商品" onChange={(e) => update(i, { root: e.target.value })}
                          className="rounded border border-line bg-paper px-1 py-0.5 text-ink">
                          {Object.entries(FUT_NAMES).map(([k, n]) => <option key={k} value={k}>{k} {n}</option>)}
                        </select>
                        <input type="month" value={r.ymText} aria-label="合約月份" onChange={(e) => update(i, { ymText: e.target.value })}
                          className="num rounded border border-line bg-paper px-1 py-0.5 text-ink" />
                        <span className="text-xs text-muted">XQ：{r.name}</span>
                      </div>
                    ) : <><span className="num text-muted">{r.code}</span> {r.name}</>}
                  </td>
                  <td className="num text-right">{r.kind === 'FUT' ? `${r.shares} 口` : `${fmtInt(r.shares)} 股`}<div className="text-xs text-muted">{r.board}</div></td>
                  <td className="text-right">
                    <input type="number" step="any" min={0} value={r.priceText} aria-label={`${r.code} 價格`}
                      onChange={(e) => update(i, { priceText: e.target.value })}
                      className="num w-24 rounded border border-line bg-paper px-1.5 py-0.5 text-right text-ink outline-none focus:border-link" />
                    <div className="text-xs text-muted">{r.price_source}</div>
                  </td>
                  <td className="pl-3 text-sm">{r.duplicate ? <span className="text-muted">已記錄過</span> : <span className="text-link">新的成交</span>}{r.partial ? '・部分成交' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {res?.skipped?.length > 0 && <p className="mt-2 text-muted">略過：{res.skipped.join('、')}</p>}
      {res && !err && rows.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button onClick={apply} disabled={busy || !chosen.length || badPrice} className="rounded-md bg-link px-4 py-1.5 text-white disabled:opacity-40">
            {busy ? '寫入中…' : `匯入選取的 ${chosen.length} 筆`}
          </button>
          {!chosen.length && <span className="text-muted">{rows.every((r) => r.duplicate) ? '這份檔案的成交都已記錄過，不需要匯入。' : '請勾選要匯入的列'}</span>}
          {badPrice && <span className="text-up">有勾選的列缺日期、價格或期貨月份</span>}
          <button onClick={() => { setRes(null); setRows([]); setFileName('') }} className="rounded-md border border-line px-3 py-1.5">取消</button>
        </div>
      )}
    </div>
  )
}
