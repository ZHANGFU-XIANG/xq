import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { tone } from '../lib/format'

export function PageTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <h1 className="title text-2xl md:text-[1.75rem] leading-tight">{children}</h1>
      {aside}
    </div>
  )
}

export function Section({ title, aside, children, className = '' }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`mb-8 ${className}`}>
      {title && (
        <div className="mb-3 flex items-baseline justify-between gap-3 border-b border-line pb-1.5">
          <h2 className="text-[1.05rem] font-bold">{title}</h2>
          {aside && <div className="text-sm text-muted">{aside}</div>}
        </div>
      )}
      {children}
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
    <div className="min-w-0">
      <div className="text-sm text-muted">{label}</div>
      <div className={`num text-xl font-bold md:text-2xl ${valueClass}`}>{value}</div>
      {sub && <div className="text-xs text-muted">{sub}</div>}
    </div>
  )
}
