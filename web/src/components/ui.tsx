import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { tone } from '../lib/format'

export function PageTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <h1 className="title text-xl leading-tight md:text-2xl">{children}</h1>
      {aside}
    </div>
  )
}

export function Section({ title, aside, children, className = '' }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`mb-6 overflow-hidden rounded-xl border border-line bg-paper shadow-sm ${className}`}>
      {title && (
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line px-4 py-2.5">
          <h2 className="flex items-center gap-2 text-[1rem] font-bold"><span className="h-4 w-1 rounded-full bg-link" aria-hidden />{title}</h2>
          {aside && <div className="text-sm text-muted">{aside}</div>}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-line bg-paper p-4 ${className}`}>{children}</div>
}

export function BasketTag({ basket }: { basket?: string }) {
  if (!basket) return null
  const kind = basket === 'etf' || basket.startsWith('ETF') ? 'etf' : basket === 'offense' || basket.startsWith('進攻') ? 'offense' : 'defense'
  const style = { offense: 'bg-offense', defense: 'bg-defense', etf: 'bg-etf' }[kind]
  const label = { offense: '進攻', defense: '穩健', etf: 'ETF' }[kind]
  return <span className={`inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-xs font-medium text-white ${style}`}>{label}</span>
}

export function Change({ value, suffix = '%', digits = 2 }: { value: number | null | undefined; suffix?: string; digits?: number }) {
  if (value == null) return <span className="text-muted">—</span>
  return (
    <span className={`num ${tone(value)}`}>
      {value > 0 ? '▲' : value < 0 ? '▼' : ''}
      {Math.abs(value).toFixed(digits)}
      {suffix}
    </span>
  )
}

export function StockLink({ code, name }: { code: string; name?: string }) {
  return (
    <Link to={`/stock/${code}`} className="font-medium text-ink no-underline hover:text-link">
      <span className="num text-muted">{code}</span> {name}
    </Link>
  )
}

export function Loading({ label = '載入中' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-10 text-muted" role="status">
      <span className="h-2 w-2 animate-pulse rounded-full bg-muted" />
      {label}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error)
  return <div className="rounded-md border border-up/40 bg-up/5 px-3 py-2 text-sm text-up">{msg}</div>
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-line px-4 py-8 text-center text-muted">{children}</div>
}

export function Bullets({ items }: { items?: string[] }) {
  if (!items?.length) return null
  return (
    <ul className="space-y-1.5 pl-4 text-[0.95rem] leading-relaxed">
      {items.map((t, i) => (
        <li key={i} className="list-disc marker:text-muted">
          {t}
        </li>
      ))}
    </ul>
  )
}

export function Stat({ label, value, sub, valueClass = '' }: { label: string; value: ReactNode; sub?: ReactNode; valueClass?: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-line bg-paper px-4 py-3 shadow-sm">
      <div className="truncate text-xs font-medium text-muted">{label}</div>
      <div className={`num mt-0.5 text-xl font-bold leading-tight md:text-[1.6rem] ${valueClass}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs leading-snug text-muted">{sub}</div>}
    </div>
  )
}

/** 分頁切換（像看盤網站的「持股／可買／觀察」頁籤），附每頁筆數。 */
export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { key: T; label: string; count?: number; tone?: string }[]; value: T; onChange: (k: T) => void }) {
  return (
    <div className="flex gap-1 overflow-x-auto border-b border-line px-2 [scrollbar-width:none]" role="tablist">
      {tabs.map((t) => (
        <button key={t.key} role="tab" aria-selected={value === t.key} onClick={() => onChange(t.key)}
          className={`-mb-px flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium ${value === t.key ? 'border-link text-ink' : 'border-transparent text-muted hover:text-ink'}`}>
          {t.label}
          {t.count != null && <span className={`num rounded-full px-1.5 text-xs ${value === t.key ? 'bg-link text-white' : `bg-mist ${t.tone ?? 'text-ink-2'}`}`}>{t.count}</span>}
        </button>
      ))}
    </div>
  )
}
