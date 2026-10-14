import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Json } from '../lib/api'
import { Section } from './ui'

const STATUS: Record<string, { label: string; dot: string; text: string }> = {
  ok: { label: '正常', dot: 'bg-down', text: 'text-down' },
  running: { label: '執行中', dot: 'bg-link', text: 'text-link' },
  warn: { label: '注意', dot: 'bg-warn', text: 'text-warn' },
  error: { label: '異常', dot: 'bg-up', text: 'text-up' },
  unknown: { label: '未知', dot: 'bg-muted', text: 'text-muted' },
}

export function useHealth() {
  return useQuery<Json>({ queryKey: ['health'], queryFn: () => get('/api/health'), refetchInterval: 300_000, staleTime: 120_000 })
}

/** 左側選單底部的小狀態：雲端排程與資料是否正常。 */
export function HealthDot() {
  const { data } = useHealth()
  if (!data) return null
  const s = STATUS[data.overall] ?? STATUS.unknown
  const n = data.checks.filter((c: Json) => c.status === 'error' || c.status === 'warn').length
  return (
    <Link to="/model#health" className="flex items-center gap-2 text-xs text-white/70 no-underline hover:text-white">
      <span className={`inline-block h-2 w-2 rounded-full ${s.dot}`} aria-hidden />
      系統{s.label}{n ? `（${n} 項需注意）` : ''}
    </Link>
  )
}

/** 模型頁：每個雲端排程最後一次執行、資料新鮮度與處理方式。 */
export default function SystemHealth() {
  const { data, isLoading } = useHealth()
  const { data: sre } = useQuery<Json>({ queryKey: ['sre'], queryFn: () => get('/api/sre'), refetchInterval: 120_000 })
  return (
    <Section title="系統健康" aside={data ? `檢查時間 ${data.time}` : undefined}>
      <div id="health" />
      {isLoading && <p className="text-muted">檢查中</p>}
      {data && (
        <ul className="space-y-1.5 text-[0.95rem]">
          {data.checks.map((c: Json, i: number) => {
            const s = STATUS[c.status] ?? STATUS.unknown
            return (
              <li key={i} className="flex flex-wrap items-baseline gap-x-3">
                <span className={`inline-flex items-center gap-1.5 font-medium ${s.text}`}>
                  <span className={`inline-block h-2 w-2 rounded-full ${s.dot}`} aria-hidden />{s.label}
                </span>
                <span className="font-medium">{c.name}</span>
                <span className="text-ink-2">{c.detail}</span>
                {c.fix && <span className="w-full pl-5 text-sm text-muted">{c.fix}</span>}
              </li>
            )
          })}
        </ul>
      )}
      {sre && (
        <div className="mt-4">
          <div className="mb-1 text-sm font-medium">工程師 Agent（每 5 分鐘自我檢查・最近 {sre.last ?? '尚未執行'}）</div>
          {sre.events?.length ? (
            <ul className="space-y-1 text-sm">
              {sre.events.map((e: Json, i: number) => (
                <li key={i}><span className="num mr-2 text-muted">{e.time}</span>
                  <span className={e.level === '嚴重' ? 'font-medium text-up' : 'text-warn'}>{e.title}</span>
                  {e.detail && <span className="ml-1 text-ink-2">{e.detail}</span>}
                  {e.fixed && <span className="ml-1 text-down">→ {e.fixed}</span>}
                  {(e.refs ?? []).slice(0, 2).map((r: Json) => <a key={r.url} href={r.url} target="_blank" rel="noreferrer" className="ml-2 text-xs text-link">參考：{r.title.slice(0, 30)}</a>)}
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted">沒有異常事件。</p>}
        </div>
      )}
      <p className="mt-2 text-xs text-muted">排程失敗時會自動寄 Email 通知；GitHub 的排程可能延遲 5～30 分鐘屬正常。</p>
    </Section>
  )
}
