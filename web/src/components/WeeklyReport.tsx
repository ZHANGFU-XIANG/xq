import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { fileUrl, get, post, type Json } from '../lib/api'

/** 週報 PPT：選一週 → 彙整當週每個交易日的成交紀錄、理由、市場狀況與績效 → 產生可下載的 .pptx。 */
export default function WeeklyReport() {
  const qc = useQueryClient()
  const { data } = useQuery<Json>({ queryKey: ['weekly-reports'], queryFn: () => get('/api/weekly-reports') })
  const [today] = useState(() => new Date().toLocaleDateString('sv-SE'))  // 本地日期 YYYY-MM-DD（toISOString 是 UTC，台灣早上 8 點前會差一天）
  const weeks: Json[] = data?.weeks ?? []
  const current = weeks.find((w) => w.monday <= today && today <= w.friday) ?? weeks.filter((w) => w.monday <= today).at(-1) ?? weeks[0]
  const [week, setWeek] = useState<string>('')
  const chosen = week || current?.monday || ''
  const make = useMutation({
    mutationFn: () => post('/api/weekly-report', { week: chosen || null }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['weekly-reports'] }),
  })
  const r: Json | undefined = make.data
  return (
    <section className="mt-6 rounded-lg border border-line bg-paper p-4" aria-label="週報 PPT">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-bold">週報 PPT</h2>
        <span className="text-sm text-muted">彙整當週每個交易日的成交、理由、當天市場與事件、總資產變化，產生可直接上台的簡報</span>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <label htmlFor="weekly-pick" className="text-sm text-ink-2">週次</label>
        <select id="weekly-pick" value={chosen} onChange={(e) => setWeek(e.target.value)}
          className="rounded-md border border-line bg-paper px-2 py-1.5 text-ink">
          {weeks.map((w) => (
            <option key={w.monday} value={w.monday} disabled={w.monday > today}>
              第 {w.week_no} 週（{w.monday.slice(5).replace('-', '/')}～{w.friday.slice(5).replace('-', '/')}）{w.monday > today ? '・尚未開始' : ''}
            </option>
          ))}
        </select>
        <button disabled={make.isPending} onClick={() => make.mutate()}
          className="rounded-md bg-ink px-4 py-1.5 text-paper disabled:opacity-50">
          {make.isPending ? '製作中（約 30～60 秒）' : '產生週報'}
        </button>
      </div>
      {make.isError && <p className="mt-2 text-sm text-up">{(make.error as Error).message}</p>}
      {r && (
        <div className="mt-3 rounded-md bg-mist px-3 py-2 text-[0.95rem]">
          <div className="font-medium">{r.headline}</div>
          <div className="mt-0.5 text-sm text-ink-2">
            {r.week_label}・{r.period}・{r.trades} 筆成交・{r.slides} 頁
            {r.verified_text ? '・文字由開源 AI 撰寫，數字已核對' : '・文字採固定格式（AI 未通過數字核對或暫時無法使用）'}
          </div>
          <a href={fileUrl(r.file)} className="mt-2 inline-block rounded-md bg-link px-3 py-1.5 text-sm text-white no-underline">下載 {r.name}</a>
        </div>
      )}
      {!!data?.files?.length && (
        <div className="mt-3 text-sm">
          <h3 className="mb-1 font-medium text-muted">已產生的週報</h3>
          <ul className="space-y-0.5">
            {data.files.slice(0, 8).map((f: Json) => (
              <li key={f.file}><a href={fileUrl(f.file)}>{f.name}</a> <span className="text-muted">{f.generated_at}</span></li>
            ))}
          </ul>
        </div>
      )}
      <p className="mt-3 text-xs text-muted">成交紀錄來自網站匯入的 XQ 庫存與 AI 助理記錄；記錄成交時說明理由（例如「因為法說會利多，買進 2330 一張」），週報就會寫進去。也可以直接問 AI 助理「幫我做這週的週報」。</p>
    </section>
  )
}
