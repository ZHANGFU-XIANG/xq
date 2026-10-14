import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Empty, Loading, PageTitle, Section } from '../components/ui'
import { get, type Json } from '../lib/api'
import { liveEvery } from '../lib/live'

const LEVEL: Record<string, string> = { 嚴重: 'border-up', 注意: 'border-warn' }

function AgentStatus() {
  const { data } = useQuery<Json>({ queryKey: ['agents'], queryFn: () => get('/api/agents'), refetchInterval: 60_000 })
  if (!data?.agents) return null
  return (
    <Section title="Agent 狀態" aside={data.running ? '更新中…' : '背景每小時更新（盤中每 30 分鐘、只跑快速 Agent）'}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] text-sm">
          <thead className="text-left text-muted"><tr className="border-b border-line"><th className="py-1 font-normal">Agent</th>
            <th className="font-normal">狀態</th><th className="text-right font-normal">耗時</th><th className="font-normal pl-3">最近執行</th></tr></thead>
          <tbody>
            {data.agents.map((a: Json) => (
              <tr key={a.name} className="border-b border-line/60">
                <td className="py-1">{a.title}{a.uses_llm && <span className="ml-1 text-xs text-muted">（用 LLM）</span>}</td>
                <td className={a.ok == null ? 'text-muted' : a.ok ? 'text-down' : 'text-up'}>{a.ok == null ? '尚未執行' : a.ok ? '正常' : `失敗：${a.error ?? ''}`}</td>
                <td className="num text-right">{a.seconds != null ? `${a.seconds} 秒` : '—'}</td>
                <td className="pl-3 text-muted">{a.time ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.error && <p className="mt-1 text-sm text-up">{data.error}</p>}
    </Section>
  )
}

function Debate() {
  const { data, isLoading } = useQuery<Json>({ queryKey: ['debate'], queryFn: () => get('/api/debate'), staleTime: 30 * 60_000 })
  if (isLoading) return <Loading label="多空辯論（首次約需 1～3 分鐘）" />
  if (!data?.items) return null
  return (
    <Section title="持股與推薦的多空辯論" aside={`${data.time}・${data.llm ? 'LLM 撰寫論點' : '事實清單'}`}>
      <p className="mb-2 text-sm text-muted">{data.note}</p>
      <div className="grid items-start gap-3 md:grid-cols-2">
        {data.items.map((d: Json) => (
          <div key={d.code} className="rounded-lg border border-line p-3 text-sm">
            <div className="mb-1 font-bold">{d.code} {d.name}
              <span className="ml-2 rounded-full bg-mist px-2 py-0.5 text-xs font-medium">{d.verdict ?? '推薦'}</span>
              <span className="ml-1 text-xs text-muted">論點{d.lean}（多 {d.bull_weight}／空 {d.bear_weight}）</span></div>
            <div className="text-up">多方：{d.bull.map((b: Json) => b.text).join('；') || '—'}</div>
            <div className="text-down">空方：{d.bear.map((b: Json) => b.text).join('；') || '—'}</div>
            <div className="mt-1 text-ink-2">主要分歧：{d.key_dispute}</div>
            <div className="text-xs text-muted">什麼情況會改變結論：{d.what_changes.join('；')}</div>
          </div>
        ))}
      </div>
    </Section>
  )
}

export default function Briefing() {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const { data: b, isLoading } = useQuery<Json>({ queryKey: ['briefing'], queryFn: () => get('/api/briefing'), refetchInterval: liveEvery(120_000, 300_000) })
  const refresh = async () => {
    setBusy(true)
    try { qc.setQueryData(['briefing'], await get('/api/briefing?refresh=true')) } finally { setBusy(false) }
  }
  return (
    <>
      <PageTitle aside={<button onClick={refresh} disabled={busy} className="rounded-md bg-link px-4 py-2 text-sm font-medium text-white hover:opacity-90">{busy ? '更新中…' : '立即更新'}</button>}>
        總報告
      </PageTitle>
      {isLoading && <Loading label="統整各 Agent 結果" />}
      {b && (
        <Section title="今日重點" aside={b.time}>
          {b.sections?.length ? (
            <div className="space-y-3">
              {b.sections.map((s: Json) => (
                <div key={s.title} className={`border-l-4 pl-3 ${LEVEL[s.level] ?? 'border-line'}`}>
                  <div className="font-bold">{s.title}</div>
                  <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-ink-2">{s.lines.map((l: string, i: number) => <li key={i}>{l}</li>)}</ul>
                </div>
              ))}
            </div>
          ) : <Empty>還沒有資料，按「立即更新」。</Empty>}
          {b.failed?.length > 0 && <p className="mt-2 text-xs text-warn">部分 Agent 失敗（已略過）：{b.failed.join('；')}</p>}
          <p className="mt-3 text-xs text-muted">每天 06:40、21:30 寄出 Email；數字全部由程式計算，LLM 只負責新聞分類與辯論措辭並經過核對。</p>
        </Section>
      )}
      <Debate />
      <AgentStatus />
    </>
  )
}
