import { useSearchParams } from 'react-router-dom'
import type { Json } from '../lib/api'

export type Asset = 'STOCK' | 'ETF' | 'FUT' | 'MARKET'

export const ASSET_LABEL: Record<Asset, string> = { STOCK: '股票', ETF: 'ETF', FUT: '期貨與選擇權', MARKET: '市場與規則' }

/** 台股 ETF 代號都是 00 開頭（0050、00878…）；有 asset 欄位時以後端為準。 */
export function assetOf(x: Json | string | undefined): Asset {
  if (!x) return 'STOCK'
  if (typeof x !== 'string') {
    if (x.asset === 'OPT') return 'FUT'
    if (x.asset) return x.asset as Asset
    return assetOf(x.code)
  }
  return /^00\d/.test(x) ? 'ETF' : 'STOCK'
}

/** 目前分頁存在網址 ?asset=，切換頁面（總覽、每日建議、警示）時保持在同一類別。 */
export function useAssetTab(options: Asset[] = ['STOCK', 'ETF', 'FUT']): [Asset, (a: Asset) => void] {
  const [sp, setSp] = useSearchParams()
  const cur = (sp.get('asset') as Asset) || options[0]
  const tab = options.includes(cur) ? cur : options[0]
  return [tab, (a: Asset) => {
    const next = new URLSearchParams(sp)
    next.set('asset', a)
    next.delete('code')
    setSp(next, { replace: true })
  }]
}

const STYLE: Record<Asset, string> = {
  STOCK: 'border-offense bg-offense', ETF: 'border-etf bg-etf', FUT: 'border-fut bg-fut', MARKET: 'border-ink bg-ink',
}

export default function AssetTabs({ tab, onChange, counts = {}, options = ['STOCK', 'ETF', 'FUT'] }:
  { tab: Asset; onChange: (a: Asset) => void; counts?: Partial<Record<Asset, number>>; options?: Asset[] }) {
  return (
    <div role="tablist" aria-label="商品類別" className="sticky top-0 z-20 -mx-1 mb-6 flex flex-wrap gap-2 bg-mist/90 px-1 py-2 backdrop-blur">
      {options.map((a) => (
        <button key={a} role="tab" aria-selected={tab === a} onClick={() => onChange(a)}
          className={`rounded-full border px-4 py-1.5 text-[0.95rem] ${tab === a ? `${STYLE[a]} text-white` : 'border-line bg-paper text-ink hover:border-ink-2'}`}>
          {ASSET_LABEL[a]}
          {counts[a] != null && <span className={`num ml-1.5 rounded-full px-1.5 text-xs ${tab === a ? 'bg-white/25' : 'bg-mist text-ink-2'}`}>{counts[a]}</span>}
        </button>
      ))}
    </div>
  )
}
