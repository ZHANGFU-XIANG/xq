import { useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { post, type Json } from '../lib/api'
import { fmtInt } from '../lib/format'

type Mode = 'shot' | 'text'

/** 圖片在瀏覽器先縮到長邊 2000px（手機截圖動輒數 MB，上傳慢）。 */
async function toDataUrl(file: Blob): Promise<string> {
  const bmp = await createImageBitmap(file)
  const scale = Math.min(1, 2000 / Math.max(bmp.width, bmp.height))
  const c = document.createElement('canvas')
  c.width = Math.round(bmp.width * scale)
  c.height = Math.round(bmp.height * scale)
  c.getContext('2d')!.drawImage(bmp, 0, 0, c.width, c.height)
  return c.toDataURL('image/jpeg', 0.9)
}

/** 從 XQ 匯入：截圖（庫存或成交回報）或複製貼上庫存文字 → 解析 → 與系統比對 → 確認後寫入。 */
export default function XQImport() {
  const qc = useQueryClient()
  const [mode, setMode] = useState<Mode>('shot')
  const [text, setText] = useState('')
  const [image, setImage] = useState<string | null>(null)
  const [complete, setComplete] = useState(true)
  const [res, setRes] = useState<Json | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  async function pickImage(f: Blob | null | undefined) {
    if (!f || !f.type.startsWith('image/')) return
    setRes(null)
    setMsg('')
    try {
      setImage(await toDataUrl(f))
    } catch {
      setMsg('無法讀取這張圖片')
    }
  }
  function onPaste(e: React.ClipboardEvent) {
    const item = [...e.clipboardData.items].find((i) => i.type.startsWith('image/'))
    if (item) {
      e.preventDefault()
      setMode('shot')
      pickImage(item.getAsFile())
    }
  }
  async function check() {
    setBusy(true)
    setMsg('')
    setRes(null)
    try {
      setRes(mode === 'shot'
        ? await post('/api/xq/screenshot', { image, complete })
        : await post('/api/xq/import', { text, complete }))
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '解析失敗')
    } finally {
      setBusy(false)
    }
  }
  async function apply() {
    if (!res?.pending_action) return
    setBusy(true)
    try {
      const r = await post(`/api/actions/${res.pending_action}/confirm`)
      setMsg(r.message)
      setRes(null)
      setText('')
      setImage(null)
      qc.invalidateQueries()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '同步失敗')
    } finally {
      setBusy(false)
    }
  }

  const tabCls = (m: Mode) => `rounded-md px-3 py-1 ${mode === m ? 'bg-link text-white' : 'border border-line text-ink-2'}`
  const isFills = res?.kind === 'fills'
  return (
    <div className="rounded-lg border border-line bg-paper p-4" onPaste={onPaste}>
      <div className="mb-3 flex flex-wrap gap-2 text-sm" role="tablist">
        <button role="tab" aria-selected={mode === 'shot'} className={tabCls('shot')} onClick={() => setMode('shot')}>截圖</button>
        <button role="tab" aria-selected={mode === 'text'} className={tabCls('text')} onClick={() => setMode('text')}>貼上文字</button>
      </div>

      {mode === 'shot' ? (
        <>
          <ol className="mb-3 list-decimal space-y-0.5 pl-5 text-sm text-ink-2">
            <li>在 XQ 打開「帳務→庫存」或「成交回報」，按 Win+Shift+S 截取表格（手機可直接截圖）</li>
            <li>在這個框內按 Ctrl+V 貼上，或點下方選擇圖片</li>
            <li>按「辨識」：庫存會與系統對帳；成交回報會列出要記錄的成交。確認無誤才寫入</li>
          </ol>
          <div
            className="flex min-h-28 cursor-pointer items-center justify-center rounded-md border-2 border-dashed border-line px-3 py-3 text-sm text-muted"
            onClick={() => fileRef.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { e.preventDefault(); pickImage(e.dataTransfer.files[0]) }}
          >
            {image ? <img src={image} alt="要辨識的截圖" className="max-h-72 max-w-full rounded" /> : '貼上（Ctrl+V）、拖曳或點這裡選擇截圖'}
          </div>
          <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={(e) => pickImage(e.target.files?.[0])} />
        </>
      ) : (
        <>
          <ol className="mb-3 list-decimal space-y-0.5 pl-5 text-sm text-ink-2">
            <li>在 XQ 全球贏家打開「帳務」→「庫存」（證券）</li>
            <li>點表格任一格，按 Ctrl+A 全選、Ctrl+C 複製（連同表頭）</li>
            <li>貼到下面，按「比對」。複製不了時改用「截圖」</li>
          </ol>
          <label htmlFor="xq-text" className="sr-only">XQ 庫存表格</label>
          <textarea id="xq-text" value={text} onChange={(e) => setText(e.target.value)} rows={5}
            placeholder={'商品代碼\t商品名稱\t類別\t庫存股數\t成本均價 …'}
            className="num w-full rounded-md border border-line bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-link" />
        </>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
        <button onClick={check} disabled={busy || (mode === 'shot' ? !image : !text.trim())}
          className="rounded-md bg-link px-4 py-1.5 text-white disabled:opacity-40">
          {busy && !res ? (mode === 'shot' ? '辨識中…（約 10 秒）' : '比對中…') : mode === 'shot' ? '辨識' : '比對'}
        </button>
        <label className="inline-flex items-center gap-1.5 text-ink-2">
          <input type="checkbox" checked={complete} onChange={(e) => setComplete(e.target.checked)} />
          庫存是完整的（系統有、XQ 沒有的視為已賣出）
        </label>
      </div>
      {msg && <p className="mt-2 text-sm">{msg}</p>}

      {res && res.source === 'screenshot' && (
        <p className="mt-3 text-sm text-muted">
          辨識為「{res.kind === 'holdings' ? '庫存' : res.kind === 'fills' ? '成交回報' : '無法判斷'}」・{res.model}・請對照截圖核對數字
        </p>
      )}
      {res?.warnings?.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-sm text-warn">{res.warnings.map((w: string) => <li key={w}>{w}</li>)}</ul>
      )}
      {res?.error && <p className="mt-2 text-sm text-up">{res.error}</p>}

      {isFills && (res.trades?.length > 0 || res.duplicates?.length > 0) && (
        <div className="mt-3">
          <table className="w-full text-[0.95rem]">
            <thead className="text-left text-sm text-muted">
              <tr className="border-b border-line"><th className="py-1 font-normal">標的</th><th className="font-normal">買賣</th><th className="text-right font-normal">股數</th><th className="text-right font-normal">成交價</th></tr>
            </thead>
            <tbody>
              {res.trades.map((t: Json) => (
                <tr key={`${t.code}${t.side}${t.price}`} className="border-b border-line/60">
                  <td className="py-1.5"><span className="num text-muted">{t.code}</span> {res.read.find((r: Json) => r.code === t.code)?.name}</td>
                  <td className={t.side === 'BUY' ? 'text-up' : 'text-down'}>{t.side === 'BUY' ? '買進' : '賣出'}{t.margin ? '（融資）' : ''}</td>
                  <td className="num text-right">{fmtInt(t.shares)}</td>
                  <td className="num text-right">{t.price}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {res.duplicates?.length > 0 && <p className="mt-2 text-sm text-muted">今天已記錄過、不再重複：{res.duplicates.join('、')}</p>}
        </div>
      )}
      {isFills && res.skipped?.length > 0 && <p className="mt-2 text-sm text-muted">略過：{res.skipped.join('、')}</p>}

      {res?.diffs && (
        <div className="mt-4">
          <table className="w-full text-[0.95rem]">
            <thead className="text-left text-sm text-muted">
              <tr className="border-b border-line"><th className="py-1 font-normal">標的</th><th className="text-right font-normal">系統股數</th><th className="text-right font-normal">XQ 股數</th><th className="pl-4 font-normal">狀態</th></tr>
            </thead>
            <tbody>
              {res.diffs.map((d: Json) => (
                <tr key={d.code} className="border-b border-line/60">
                  <td className="py-1.5"><span className="num text-muted">{d.code}</span> {d.name}</td>
                  <td className="num text-right">{fmtInt(d.system)}</td>
                  <td className="num text-right">{fmtInt(d.xq)}</td>
                  <td className={`pl-4 ${d.status === '一致' ? 'text-down' : 'text-warn'}`}>{d.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {res?.in_sync && <p className="mt-3 text-down">{isFills ? '這些成交都已記錄過，不需要調整。' : '系統紀錄與 XQ 一致，不需要調整。'}</p>}
      {res?.pending_action && (
        <div className="mt-3 rounded-md border-2 border-link/50 px-3 py-2 text-sm">
          <div className="mb-2">{res.summary}</div>
          <div className="flex gap-2">
            <button onClick={apply} disabled={busy} className="rounded-md bg-link px-3 py-1 text-white disabled:opacity-50">確認寫入</button>
            <button onClick={() => setRes(null)} className="rounded-md border border-line px-3 py-1">取消</button>
          </div>
        </div>
      )}
    </div>
  )
}
