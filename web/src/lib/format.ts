export const fmtInt = (v: number | null | undefined) => (v == null ? '—' : Math.round(v).toLocaleString('zh-TW'))

export const fmtNum = (v: number | null | undefined, d = 2) =>
  v == null ? '—' : v.toLocaleString('zh-TW', { minimumFractionDigits: d, maximumFractionDigits: d })

/** v 已是百分比數值（例如 1.23 代表 1.23%） */
export const fmtPct = (v: number | null | undefined, d = 2, sign = true) =>
  v == null ? '—' : `${sign && v > 0 ? '+' : ''}${v.toFixed(d)}%`

/** v 為比例（0.0123 代表 1.23%） */
export const fmtRatio = (v: number | null | undefined, d = 1, sign = false) =>
  v == null ? '—' : fmtPct(v * 100, d, sign)

export const fmtWan = (v: number | null | undefined) => (v == null ? '—' : `${(v / 1e4).toLocaleString('zh-TW', { maximumFractionDigits: 0 })} 萬`)

export const tone = (v: number | null | undefined) =>
  v == null || v === 0 ? 'text-ink-2' : v > 0 ? 'text-up' : 'text-down'

export const qty = (lots: number, odd: number) => `${lots ? `${lots} 張` : ''}${lots && odd ? ' + ' : ''}${odd ? `${odd} 股` : ''}` || '0 股'
