import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useChat } from '../components/Chat'
import KLine from '../components/KLine'
import { BasketTag, Bullets, Empty, ErrorNote, Loading, PageTitle, Panel, Section, StockLink } from '../components/ui'
import AssetTabs, { ASSET_LABEL, assetOf, useAssetTab } from '../components/AssetTabs'
import Derivatives from '../components/Derivatives'
import { fileUrl, get, type Json } from '../lib/api'
import { fmtInt, fmtPct, qty } from '../lib/format'

const TABS = [
  { key: 'why_stock', label: '為何選這檔' },
  { key: 'why_qty', label: '為何這個張數' },
  { key: 'more_less', label: '多買／少買' },
  { key: 'exit_plan', label: '出場計畫' },
  { key: 'risks', label: '風險' },
]

function OrderDetail({ o, note }: { o: Json; note?: string }) {
  const [tab, setTab] = useState('why_stock')
  const chat = useChat()
  if (o.action === 'SELL')
    return (
      <div>
        <h2 className="title mb-1 text-xl">賣出 <StockLink code={o.code} name={o.name} /></h2>
        <p className="num mb-4 text-ink-2">{qty(o.lots, o.odd)}，限價 {o.limit_price}，賣出後剩 {fmtInt(o.holding_after)} 股</p>
        <KLine code={o.code} />
        <Section title="賣出原因" className="mt-6"><p>{o.why_sell}</p></Section>
        <Section title="為何賣這個股數"><p>{o.why_qty}</p></Section>
      </div>
    )
  const items: string[] =
    tab === 'more_less'
      ? [...o.why_more.map((x: string) => `多買：${x}`), ...o.why_less.map((x: string) => `少買：${x}`)]
      : tab === 'exit_plan'
        ? [...o.exit_plan, `來回交易成本約 ${o.round_trip_cost_pct}%，損益兩平價 ${o.breakeven} 元`]
        : o[tab] ?? []
  return (
    <div>
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <BasketTag basket={o.basket} />
        <h2 className="title text-xl">買進 <StockLink code={o.code} name={o.name} /></h2>
      </div>
      <p className="num mb-1 text-lg">
        {qty(o.lots, o.odd)}　限價 <b>{o.limit_price}</b>　停損 <b className="text-down">{o.stop}</b>　約 {fmtInt(o.amount)} 元（{fmtPct(o.weight_pct, 1, false)}）
        {o.margin && <span className="ml-2 rounded bg-warn/15 px-1.5 text-sm text-warn">融資</span>}
      </p>
      {note && <p className="mb-4 font-serif text-[1.02rem] leading-relaxed text-ink-2">{note}</p>}
      <KLine code={o.code} />
      <div className="mt-6 flex flex-wrap gap-1 border-b border-line" role="tablist">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-[0.95rem] ${tab === t.key ? 'border-ink font-medium' : 'border-transparent text-muted hover:text-ink'}`}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="py-4" role="tabpanel"><Bullets items={items} /></div>
      <button onClick={() => chat.ask(`${o.code} ${o.name} 為什麼建議${o.action === 'BUY' ? '買' : '賣'}這個張數？有什麼風險？`)}
        className="text-sm text-link hover:underline">問 AI 助理這筆建議</button>
    </div>
  )
}

export default function Daily() {
  const { key } = useParams()
  const [sp, setSp] = useSearchParams()
  const [tab, setTab] = useAssetTab()
  const nav = useNavigate()
  const { data: list } = useQuery<Json[]>({ queryKey: ['reports'], queryFn: () => get('/api/reports') })
  const { data: r, isLoading, error } = useQuery<Json>({
    queryKey: ['report', key ?? 'latest'],
    queryFn: () => get(`/api/report${key ? `?key=${encodeURIComponent(key)}` : ''}`),
  })
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!r) return <Empty>還沒有報告。</Empty>
  const all: Json[] = r.orders
  const orders: Json[] = all.filter((o) => assetOf(o) === tab)
  const futN = (r.derivatives?.orders?.length ?? 0) + (r.derivatives?.hedge ? 1 : 0)
  const sel = orders.find((o) => o.code === sp.get('code')) ?? orders[0]
  const notes: Record<string, string> = Object.fromEntries((r.text?.order_notes ?? []).map((n: Json) => [n.code, n.note]))
  return (
    <>
      <PageTitle
        aside={
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="sr-only" htmlFor="report-pick">選擇報告</label>
            <select id="report-pick" value={r.key} onChange={(e) => nav(`/daily/${e.target.value}`)}
              className="rounded-md border border-line bg-paper px-2 py-1.5 text-ink">
              {list?.map((f) => <option key={f.key} value={f.key}>{f.date} {f.title}</option>)}
            </select>
            {Object.entries(r.files ?? {}).map(([ext, p]) => (
              <a key={ext} href={fileUrl(p as string)} className="rounded-md border border-line bg-paper px-2.5 py-1.5 no-underline hover:border-link">
                下載 {ext === 'docx' ? 'Word' : ext === 'pptx' ? '簡報' : '下單清單'}
              </a>
            ))}
          </div>
        }
      >
        {r.date} {r.title}
      </PageTitle>
      <p className="mb-6 max-w-3xl leading-relaxed text-ink-2">{r.text?.allocation_analysis}</p>
      <AssetTabs tab={tab} onChange={setTab}
        counts={{ STOCK: all.filter((o) => assetOf(o) === 'STOCK').length, ETF: all.filter((o) => assetOf(o) === 'ETF').length, FUT: futN }} />
      {tab === 'FUT' ? (
        r.derivatives ? <Derivatives d={r.derivatives} /> : <Empty>這份報告產生時還沒有期貨建議功能。最新的期貨建議請看總覽或期貨頁。</Empty>
      ) : !orders.length ? (
        <Empty>這份報告沒有{ASSET_LABEL[tab]}的交易建議。</Empty>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[17rem_1fr]">
          <nav aria-label="建議清單" className="space-y-1.5 lg:sticky lg:top-6 lg:self-start">
            {orders.map((o) => {
              const active = sel?.code === o.code
              return (
                <button key={o.code} onClick={() => setSp({ asset: tab, code: o.code })}
                  className={`flex w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left ${active ? 'border-ink bg-paper' : 'border-transparent hover:bg-paper'}`}>
                  <span className="min-w-0">
                    <span className="flex items-center gap-1.5">
                      {o.action === 'BUY' ? <BasketTag basket={o.basket} /> : <span className="rounded-full bg-down px-2 py-0.5 text-xs text-white">賣出</span>}
                      <span className="truncate font-medium">{o.name}</span>
                    </span>
                    <span className="num text-xs text-muted">{o.code}　{qty(o.lots, o.odd)}</span>
                  </span>
                  <span className="num text-sm">{o.limit_price}</span>
                </button>
              )
            })}
          </nav>
          <Panel className="min-w-0">{sel && <OrderDetail o={sel} note={notes[sel.code]} />}</Panel>
        </div>
      )}
    </>
  )
}
